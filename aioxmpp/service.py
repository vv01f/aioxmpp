########################################################################
# File name: service.py
# This file is part of: aioxmpp
#
# LICENSE
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program.  If not, see
# <http://www.gnu.org/licenses/>.
#
########################################################################
"""
:mod:`~aioxmpp.service` --- Utilities for implementing :class:`~.Client` services
#################################################################################

Protocol extensions or in general support for parts of the XMPP protocol are
implemented using :class:`Service` classes, or rather, classes which use the
:class:`Meta` metaclass.

Both of these are provided in this module. To reduce the boilerplate required
to develop services, :ref:`decorators <api-aioxmpp.service-decorators>` are
provided which can be used to easily register coroutines and functions as
stanza handlers, filters and others.

.. autoclass:: Service

.. _api-aioxmpp.service-decorators:

Decorators and Descriptors
==========================

These decorators provide special functionality when used on methods of
:class:`Service` subclasses.

.. note::

   These decorators work only on methods declared on :class:`Service`
   subclasses, as their functionality are implemented in cooperation with the
   :class:`Meta` metaclass and :class:`Service` itself.

.. autodecorator:: iq_handler

.. autodecorator:: message_handler

.. autodecorator:: presence_handler

.. autodecorator:: inbound_message_filter()

.. autodecorator:: inbound_presence_filter()

.. autodecorator:: outbound_message_filter()

.. autodecorator:: outbound_presence_filter()

.. autodecorator:: depsignal

.. autodecorator:: depfilter

.. seealso::

   :class:`~.disco.register_feature`
      For a descriptor (see below) which allows to register a Service Discovery
      feature when the service is instantiated.

   :class:`~.disco.mount_as_node`
      For a descriptor (see below) which allows to register a Service Discovery
      node when the service is instantiated.

Test functions
--------------

.. autofunction:: is_iq_handler

.. autofunction:: is_message_handler

.. autofunction:: is_presence_handler

.. autofunction:: is_inbound_message_filter

.. autofunction:: is_inbound_presence_filter

.. autofunction:: is_outbound_message_filter

.. autofunction:: is_outbound_presence_filter

.. autofunction:: is_depsignal_handler

.. autofunction:: is_depfilter_handler

Creating your own decorators
----------------------------

Sometimes, when you create your own service, it makes sense to create own
decorators which depending services can use to make easy use of some features
of your service.

.. note::

   Remember that it isn’t necessary to create custom decorators to simply
   connect a method to a signal exposed by another service. Users of that
   service should be using :func:`depsignal` instead.

The key part is the :class:`HandlerSpec` object. It specifies the effect the
decorator has on initialisation and shutdown of the service. To add a
:class:`HandlerSpec` to a decorated method, use :func:`add_handler_spec` in the
implementation of your decorator.

.. autoclass:: HandlerSpec(key, is_unique=True, require_deps=[])

.. autofunction:: add_handler_spec

Creating your own descriptors
-----------------------------

Sometimes a decorator is not the right tool for the job, because with what you
attempt to achieve, there’s simply no relationship to a method.

In this case, subclassing :class:`Descriptor` is the way to go. It provides an
abstract base class implementing a :term:`descriptor`. Using a
:class:`Descriptor` subclass, you can create objects for each individual
service instance using the descriptor, including cleanup.

.. autoclass:: Descriptor

Metaclass
=========

.. autoclass:: Meta()


"""

import abc
import asyncio
import collections
import contextlib
import copy
import logging
import warnings
import weakref

import aioxmpp.callbacks
import aioxmpp.stream


def automake_magic_attr(obj):
    obj._aioxmpp_service_handlers = getattr(
        obj, "_aioxmpp_service_handlers", set()
    )
    return obj._aioxmpp_service_handlers


def get_magic_attr(obj):
    return obj._aioxmpp_service_handlers


def has_magic_attr(obj):
    return hasattr(
        obj, "_aioxmpp_service_handlers"
    )


class Descriptor(metaclass=abc.ABCMeta):
    """
    Abstract base class for resource managing descriptors on :class:`Service`
    classes.

    While resources such as callback slots can easily be managed with
    decorators (see above), because they are inherently related to the method
    they use, others cannot. A :class:`Descriptor` provides a method to
    initialise a context manager. The context manager is entered when the
    service is initialised and left when the service is shut down, thus
    providing a way for the :class:`Descriptor` to manage the resource
    associated with it.

    The result from entering the context manager is accessible by reading the
    attribute the descriptor is bound to.

    Subclasses must implement the following:

    .. automethod:: init_cm

    Subclasses may override the following to modify the default behaviour:

    .. autoattribute:: required_dependencies

    .. automethod:: add_to_stack

    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._data = weakref.WeakKeyDictionary()

    @property
    def required_dependencies(self):
        """
        Iterable of services which must be declared as dependencies on a class
        using this descriptor.

        The default implementation returns an empty list.
        """
        return []

    @abc.abstractmethod
    def init_cm(self, instance):
        """
        Create and return a :term:`context manager`.

        :param instance: The service instance for which the CM is used.
        :return: A context manager managing the resource.

        The context manager is responsible for acquiring, initialising,
        destructing and releasing the resource managed by this descriptor.

        The returned context manager is not stored anywhere in the descriptor,
        it is the responsibility of the caller to register it appropriately.
        """

    def add_to_stack(self, instance, stack):
        """
        Get the context manager for the service `instance` and push it to the
        context manager `stack`.

        :param instance: The service to get the context manager for.
        :type instance: :class:`Service`
        :param stack: The context manager stack to push the CM onto.
        :type stack: :class:`contextlib.ExitStack`
        :return: The object returned by the context manager on enter.

        If a context manager has already been created for `instance`, it is
        re-used.

        On subsequent calls to :meth:`__get__` for the given `instance`, the
        return value of this method will be returned, that is, the value
        obtained from entering the context.
        """

        cm = self.init_cm(instance)
        obj = stack.enter_context(cm)
        self._data[instance] = cm, obj
        return obj

    def __get__(self, instance, owner):
        if instance is None:
            return self
        try:
            cm, obj = self._data[instance]
        except KeyError:
            raise AttributeError(
                "resource manager descriptor has not been initialised"
            )
        return obj


class DependencyGraphNode:
    def __init__(self):
        self.class_ = None

    def __repr__(self):
        return "DependencyGraphNode({!r})".format(self.class_)


class DependencyGraph:

    def __init__(self, edges=None, nodes=None):
        if edges is None:
            edges = []
        if nodes is None:
            nodes = []
        self._edges = set(edges)
        self._nodes = set(nodes)

    def __deepcopy__(self, memo):
        return DependencyGraph(self._edges, self._nodes)

    def add_node(self, node):
        self._nodes.add(node)

    def add_edge(self, from_, to):
        self._edges.add((from_, to))

    def toposort(self):
        edges = collections.defaultdict(lambda: set())
        for from_, to in self._edges:
            edges[from_].add(to)

        sorted_ = []
        marked = set()
        done = set()

        def visit(node):
            if node in marked:
                raise ValueError("dependency loop in service definitions")
            if node in done:
                return
            done.add(node)
            marked.add(node)
            for dep in edges[node]:
                visit(dep)
            marked.remove(node)
            sorted_.append(node)

        for node in self._nodes:
            visit(node)

        return sorted_


class Meta(abc.ABCMeta):
    """
    The metaclass for services. The :class:`Service` class uses it and in
    general you should just inherit from :class:`Service` and define the
    dependency attributes as needed.

    Only use :class:`Meta` explicitely if you know what you are doing,
    and you most likely do not. :class:`Meta` is internal API and may
    change at any point.

    Services have dependencies. A :class:`Meta` instance (i.e. a service class)
    can declare dependencies using the following attributes.

    .. attribute:: ORDER_BEFORE

       An iterable of :class:`Service` classes before which the class which is
       currently being declared needs to be instanciated.

       Thus, any service which occurs in :attr:`ORDER_BEFORE` will be
       instanciated *after* this class (if at all). Think of it as "*this*
       class is ordered *before* the classes in this attribute".

       .. versionadded:: 0.3

    .. attribute:: SERVICE_BEFORE

       Before 0.3, this was the name of the :attr:`ORDER_BEFORE` attribute. It
       is still supported, but use emits a :data:`DeprecationWarning`. It must
       not be mixed with :attr:`ORDER_BEFORE` or :attr:`ORDER_AFTER` on a class
       declaration, or the declaration will raise :class:`ValueError`.

       .. deprecated:: 0.3

          Support for this attribute will be removed in 1.0; starting with 1.0,
          using this attribute will raise a :class:`TypeError` on class
          declaration and a :class:`AttributeError` when accessing it on a
          class or instance.

    .. attribute:: ORDER_AFTER

       An iterable of :class:`Service` classes which will be instanciated
       *before* the class which is being declraed.

       Classes which are declared in this attribute are always instanciated
       before this class is instantiated. Think of it as "*this* class is
       ordered *after* the classes in this attribute".

       .. versionadded:: 0.3

    .. attribute:: SERVICE_AFTER

       Before 0.3, this was the name of the :attr:`ORDER_AFTER` attribute. It
       is still supported, but use emits a :data:`DeprecationWarning`. It must
       not be mixed with :attr:`ORDER_BEFORE` or :attr:`ORDER_AFTER` on a class
       declaration, or the declaration will raise :class:`ValueError`.

       .. deprecated:: 0.3

          See :attr:`SERVICE_BEFORE` for details on the deprecation cycle.

    Further, the following attributes are generated:

    .. attribute:: PATCHED_ORDER_AFTER

       An iterable of :class:`Service` classes. This includes all
       classes in :attr:`ORDER_AFTER` and all classes which specify the class
       in :attr:`ORDER_BEFORE`.

       This is primarily used internally to handle :attr:`ORDER_BEFORE` when
       summoning services.

       It is an error to manually define :attr:`PATCHED_ORDER_AFTER` in a class
       definition, doing so will raise a :class:`TypeError`.

       .. versionadded:: 0.9

    .. attribute:: _DEPGRAPH_NODE

       An internal token used for topological ordering. Consider this
       name reserved by the metaclass.

       It is an error to manually define :attr:`_DEPGRAPH_NODE` in a class
       definition, doing so will raise a :class:`TypeError`.

       .. versionadded:: 0.9

    .. versionchanged:: 0.9

       The :attr:`ORDER_AFTER` and :attr:`ORDER_BEFORE` attribtes do not
       change after class creation. In earlier versions they contained
       the transitive completion of the dependency relation.

    Dependency relationships must not have cycles; a cycle results in a
    :class:`ValueError` when the class causing the cycle is declared.

    .. note::

      Subclassing instances of :class:`Meta` is forbidden. Trying to do so
      will raise a :class:`TypeError`

      .. versionchanged:: 0.9

    Example::

        class Foo(metaclass=service.Meta):
            pass

        class Bar(metaclass=service.Meta):
            ORDER_BEFORE = [Foo]

        class Baz(metaclass=service.Meta):
            ORDER_BEFORE = [Bar]

        class Fourth(metaclass=service.Meta):
            ORDER_BEFORE = [Bar]

    ``Baz`` and ``Fourth`` will be instanciated before ``Bar`` and ``Bar`` will
    be instanciated before ``Foo``. There is no dependency relationship between
    ``Baz`` and ``Fourth``.

    """

    __dependency_graph = DependencyGraph()
    __service_order = {}

    def __new__(mcls, name, bases, namespace, inherit_dependencies=True):
        if "SERVICE_BEFORE" in namespace or "SERVICE_AFTER" in namespace:
            if "ORDER_BEFORE" in namespace or "ORDER_AFTER" in namespace:
                raise ValueError("declaration mixes old and new ordering "
                                 "attribute names (SERVICE_* vs. ORDER_*)")
            warnings.warn(
                "SERVICE_BEFORE/AFTER used on class; use ORDER_BEFORE/AFTER",
                DeprecationWarning)
            try:
                namespace["ORDER_BEFORE"] = namespace.pop("SERVICE_BEFORE")
            except KeyError:
                pass
            try:
                namespace["ORDER_AFTER"] = namespace.pop("SERVICE_AFTER")
            except KeyError:
                pass

        if "PATCHED_ORDER_AFTER" in namespace:
            raise TypeError(
                "PATCHED_ORDER_AFTER must not be defined manually. "
                "it is supplied automatically by the metaclass."
            )

        if "_DEPGRAPH_NODE" in namespace:
            raise TypeError(
                "_DEPGRAPH_NODE must not be defined manually. "
                "it is supplied automatically by the metaclass."
            )

        for base in bases:
            if isinstance(base, Meta) and base is not Service:
                raise TypeError(
                    "subclassing services is prohibited."
                )

        for base in bases:
            if hasattr(base, "SERVICE_HANDLERS") and base.SERVICE_HANDLERS:
                raise TypeError(
                    "inheritance from service class with handlers is forbidden"
                )
            if (hasattr(base, "SERVICE_DESCRIPTORS") and
                    base.SERVICE_DESCRIPTORS):
                raise TypeError(
                    "inheritance from service class with descriptors is "
                    "forbidden"
                )

        namespace["ORDER_BEFORE"] = frozenset(
            namespace.get("ORDER_BEFORE", ()))
        namespace["ORDER_AFTER"] = frozenset(
            namespace.get("ORDER_AFTER", ()))
        namespace["PATCHED_ORDER_AFTER"] = namespace["ORDER_AFTER"]

        new_deps = copy.deepcopy(mcls.__dependency_graph)

        depgraph_node = namespace["_DEPGRAPH_NODE"] = DependencyGraphNode()
        new_deps.add_node(depgraph_node)
        for cls in namespace["ORDER_AFTER"]:
            new_deps.add_edge(depgraph_node, cls._DEPGRAPH_NODE)
        for cls in namespace["ORDER_BEFORE"]:
            new_deps.add_edge(cls._DEPGRAPH_NODE, depgraph_node)
        sorted_ = new_deps.toposort()
        mcls.__dependency_graph = new_deps
        mcls.__service_order = dict(
            (node, i) for i, node in enumerate(sorted_)
        )

        SERVICE_HANDLERS = []
        existing_handlers = set()

        for attr_name, attr_value in namespace.items():
            if not has_magic_attr(attr_value):
                continue

            new_handlers = get_magic_attr(attr_value)

            unique_handlers = {
                spec.key
                for spec in new_handlers
                if spec.is_unique
            }

            conflicting = unique_handlers & existing_handlers
            if conflicting:
                key = next(iter(conflicting))
                obj = next(iter(
                    obj
                    for obj_key, obj in SERVICE_HANDLERS
                    if obj_key == key
                ))

                raise TypeError(
                    "handler conflict between {!r} and {!r}: "
                    "both want to use {!r}".format(
                        obj,
                        attr_value,
                        key,
                    )
                )

            existing_handlers |= unique_handlers

            for spec in new_handlers:
                missing = spec.require_deps - namespace["ORDER_AFTER"]
                if missing:
                    raise TypeError(
                        "decorator requires dependency {!r} "
                        "but it is not declared".format(
                            next(iter(missing))
                        )
                    )

                SERVICE_HANDLERS.append(
                    (spec.key, attr_value)
                )

        namespace["SERVICE_HANDLERS"] = tuple(SERVICE_HANDLERS)

        SERVICE_DESCRIPTORS = []

        for attr_name, attr_value in namespace.items():
            if not isinstance(attr_value, Descriptor):
                continue

            missing = set(attr_value.required_dependencies) - \
                namespace["ORDER_AFTER"]
            if missing:
                raise TypeError(
                    "descriptor requires dependency {!r} "
                    "but it is not declared".format(
                        next(iter(missing)),
                    )
                )

            SERVICE_DESCRIPTORS.append(attr_value)

        namespace["SERVICE_DESCRIPTORS"] = SERVICE_DESCRIPTORS

        return super().__new__(mcls, name, bases, namespace)

    def __init__(self, name, bases, namespace, inherit_dependencies=True):
        super().__init__(name, bases, namespace)
        for cls in self.ORDER_BEFORE:
            cls.PATCHED_ORDER_AFTER |= frozenset([self])
        self._DEPGRAPH_NODE.class_ = self

    @property
    def SERVICE_BEFORE(self):
        return self.ORDER_BEFORE

    @property
    def SERVICE_AFTER(self):
        return self.ORDER_AFTER

    def __lt__(self, other):
        return (self.__service_order[self._DEPGRAPH_NODE] <
                self.__service_order[other._DEPGRAPH_NODE])

    def __le__(self, other):
        return (self.__service_order[self._DEPGRAPH_NODE] <=
                self.__service_order[other._DEPGRAPH_NODE])


class Service(metaclass=Meta):
    """
    A :class:`Service` is used to implement XMPP or XEP protocol parts, on top
    of the more or less fixed stanza handling implemented in
    :mod:`aioxmpp.node` and :mod:`aioxmpp.stream`.

    :class:`Service` is a base class which can be used by extension developers
    to implement support for custom or standardized protocol extensions. Some
    of the features for which :mod:`aioxmpp` has support are also implemented
    using :class:`Service` subclasses.

    `client` must be a :class:`~.Client` to which the service will be attached.
    The `client` cannot be changed later, for the sake of simplicity.

    `logger_base` may be a :class:`logging.Logger` instance or :data:`None`. If
    it is :data:`None`, a logger is automatically created, by taking the fully
    qualified name of the :class:`Service` subclass which is being
    instanciated. Otherwise, the logger is passed to :meth:`derive_logger` and
    the result is used as value for the :attr:`logger` attribute.

    To implement your own service, derive from :class:`Service`. If your
    service depends on other services (such as :mod:`aioxmpp.pubsub` or
    :mod:`aioxmpp.disco`), these dependencies *must* be declared as documented
    in the service meta class :class:`Meta`.

    To stay forward compatible, accept arbitrary keyword arguments and pass
    them down to :class:`Service`. As it is not possible to directly pass
    arguments to :class:`Service`\ s on construction (due to the way
    :meth:`aioxmpp.Client.summon` works), there is no need for you
    to introduce custom arguments, and thus there should be no conflicts.

    .. note::

       Inheritance from classes which subclass :class:`Service` is forbidden.

       .. versionchanged:: 0.9

    .. autoattribute:: client

    .. autoattribute:: dependencies

    .. automethod:: derive_logger

    .. automethod:: shutdown
    """

    def __init__(self, client, *, logger_base=None, dependencies={}):
        super().__init__()
        self.__context = contextlib.ExitStack()
        self.__client = client
        self.__dependencies = dependencies

        if logger_base is None:
            self.logger = logging.getLogger(".".join([
                type(self).__module__, type(self).__qualname__
            ]))
        else:
            self.logger = self.derive_logger(logger_base)

        for (handler_cm, additional_args), obj in self.SERVICE_HANDLERS:
            self.__context.enter_context(
                handler_cm(self,
                           self.__client.stream,
                           obj.__get__(self, type(self)),
                           *additional_args)
            )

        for obj in self.SERVICE_DESCRIPTORS:
            obj.add_to_stack(self, self.__context)

    def derive_logger(self, logger):
        """
        Return a child of `logger` specific for this instance. This is called
        after :attr:`client` has been set, from the constructor.

        The child name is calculated by the default implementation in a way
        specific for aioxmpp services; it is not meant to be used by
        non-:mod:`aioxmpp` classes; do not rely on the way how the child name
        is calculated.
        """
        parts = type(self).__module__.split(".")[1:]
        if parts[-1] == "service" and len(parts) > 1:
            del parts[-1]

        return logger.getChild(".".join(
            parts+[type(self).__qualname__]
        ))

    @property
    def client(self):
        """
        The client to which the :class:`Service` is bound. This attribute is
        read-only.

        If the service has been shut down using :meth:`shutdown`, this reads as
        :data:`None`.
        """
        return self.__client

    @property
    def dependencies(self):
        """
        When the service is instantiated through
        :meth:`~.Client.summon`, this attribute holds a mapping which maps the
        service classes contained in the :attr:`~.Meta.ORDER_AFTER` attribute
        to the respective instances related to the :attr:`client`.

        This is the preferred way to obtain dependencies specified via
        :attr:`~.Meta.ORDER_AFTER`.
        """
        return self.__dependencies

    @asyncio.coroutine
    def _shutdown(self):
        """
        Actual implementation of the shut down process.

        This *must* be called using super from inheriting classes after their
        own shutdown procedure. Inheriting classes *must* override this method
        instead of :meth:`shutdown`.
        """

    @asyncio.coroutine
    def shutdown(self):
        """
        Close the service and wait for it to completely shut down.

        Some services which are still running may depend on this service. In
        that case, the service may refuse to shut down instead of shutting
        down, by raising a :class:`RuntimeError` exception.

        .. note::

           Developers creating subclasses of :class:`Service` to implement
           services should not override this method. Instead, they should
           override the :meth:`_shutdown` method.

        """
        yield from self._shutdown()
        self.__context.close()
        self.__client = None


class HandlerSpec(collections.namedtuple(
        "HandlerSpec",
        [
            "is_unique",
            "key",
            "require_deps",
        ])):
    """
    Specification of the effects of the decorator at initalisation and shutdown
    time.

    :param key: Context manager and arguments pair.
    :type key: pair
    :param is_unique: Whether multiple identical `key` values are allowed on a
                      single class.
    :type is_unique: :class:`bool`
    :param require_deps: Dependent services which are required for the
                         decorator to work.
    :type require_deps: iterable of :class:`Service` classes

    During initialisation of the :class:`Service` which has a method using a
    given handler spec, the first part of the `key` pair is called with the
    service instance as first, the client :class:`StanzaStream` as second and
    the bound method as third argument. The second part of the `key` is
    unpacked as additional positional arguments.

    The result of the call must be a context manager, which is immediately
    entered. On shutdown, the context manager is exited.

    An example use would be the following handler spec::

      HandlerSpec(
          (func, (IQType.GET, some_payload_class)),
          is_unique=True,
      )

    where ``func`` is a context manager which takes a service instance, a
    stanza stream, a bound method as well as an IQ type and a payload class. On
    enter, the context manager would register the method it received as third
    argument on the stanza stream (second argument) as handler for the given IQ
    type and payload class (fourth and fifth arguments).

    If `is_unique` is true and several methods have :class:`HandlerSpec`
    objects with the same `key`, :class:`TypeError` is raised at class
    definition time.

    If at class definition time any of the dependent classes in `require_deps`
    are not declared using the order attributes (see :class:`Meta`), a
    :class:`TypeError` is raised.
    """

    def __new__(cls, key, is_unique=True, require_deps=()):
        return super().__new__(cls, is_unique, key, frozenset(require_deps))


def add_handler_spec(f, handler_spec):
    """
    Attach a handler specification (see :class:`HandlerSpec`) to a function.

    :param f: Function to attach the handler specification to.
    :param handler_spec: Handler specification to attach to the function.
    :type handler_spec: :class:`HandlerSpec`

    This uses a private attribute, whose exact name is an implementation
    detail. The `handler_spec` is stored in a :class:`set` bound to the
    attribute.
    """
    automake_magic_attr(f).add(handler_spec)


def _apply_iq_handler(instance, stream, func, type_, payload_cls):
    return aioxmpp.stream.iq_handler(stream, type_, payload_cls, func)


def _apply_presence_handler(instance, stream, func, type_, from_):
    return aioxmpp.stream.presence_handler(stream, type_, from_, func)


def _apply_inbound_message_filter(instance, stream, func):
    return aioxmpp.stream.stanza_filter(
        stream.service_inbound_message_filter,
        func,
        type(instance),
    )


def _apply_inbound_presence_filter(instance, stream, func):
    return aioxmpp.stream.stanza_filter(
        stream.service_inbound_presence_filter,
        func,
        type(instance),
    )


def _apply_outbound_message_filter(instance, stream, func):
    return aioxmpp.stream.stanza_filter(
        stream.service_outbound_message_filter,
        func,
        type(instance),
    )


def _apply_outbound_presence_filter(instance, stream, func):
    return aioxmpp.stream.stanza_filter(
        stream.service_outbound_presence_filter,
        func,
        type(instance),
    )


def _apply_connect_depsignal(instance, stream, func, dependency, signal_name,
                             mode):
    if dependency is aioxmpp.stream.StanzaStream:
        dependency = instance.client.stream
    elif dependency is aioxmpp.node.Client:
        dependency = instance.client
    else:
        dependency = instance.dependencies[dependency]
    signal = getattr(dependency, signal_name)
    if mode is None:
        return signal.context_connect(func)
    else:
        return signal.context_connect(func, mode)


def _apply_connect_depfilter(instance, stream, func, dependency, filter_name):
    if dependency is aioxmpp.stream.StanzaStream:
        dependency = instance.client.stream
    else:
        dependency = instance.dependencies[dependency]
    filter_ = getattr(dependency, filter_name)
    return filter_.context_register(func, type(instance))


def iq_handler(type_, payload_cls):
    """
    Register the decorated coroutine function as IQ request handler.

    :param type_: IQ type to listen for
    :type type_: :class:`~.IQType`
    :param payload_cls: Payload XSO class to listen for
    :type payload_cls: :class:`~.XSO` subclass
    :raise TypeError: if the decorated object is not a coroutine function

    .. seealso::

       :meth:`~.StanzaStream.register_iq_request_coro`
          for more details on the `type_` and `payload_cls` arguments

    """

    def decorator(f):
        if not asyncio.iscoroutinefunction(f):
            raise TypeError("a coroutine function is required")

        add_handler_spec(
            f,
            HandlerSpec(
                (_apply_iq_handler, (type_, payload_cls)),
                require_deps=()
            )
        )
        return f
    return decorator


def message_handler(type_, from_):
    """
    Deprecated alias of :func:`.dispatcher.message_handler`.

    .. deprecated:: 0.9
    """
    import aioxmpp.dispatcher
    return aioxmpp.dispatcher.message_handler(type_, from_)


def presence_handler(type_, from_):
    """
    Deprecated alias of :func:`.dispatcher.presence_handler`.

    .. deprecated:: 0.9
    """
    import aioxmpp.dispatcher
    return aioxmpp.dispatcher.presence_handler(type_, from_)


def inbound_message_filter(f):
    """
    Register the decorated function as a service-level inbound message filter.

    :raise TypeError: if the decorated object is a coroutine function

    .. seealso::

       :class:`StanzaStream`
          for important remarks regarding the use of stanza filters.

    """

    if asyncio.iscoroutinefunction(f):
        raise TypeError(
            "inbound_message_filter must not be a coroutine function"
        )

    add_handler_spec(
        f,
        HandlerSpec(
            (_apply_inbound_message_filter, ())
        ),
    )
    return f


def inbound_presence_filter(f):
    """
    Register the decorated function as a service-level inbound presence filter.

    :raise TypeError: if the decorated object is a coroutine function

    .. seealso::

       :class:`StanzaStream`
          for important remarks regarding the use of stanza filters.

    """

    if asyncio.iscoroutinefunction(f):
        raise TypeError(
            "inbound_presence_filter must not be a coroutine function"
        )

    add_handler_spec(
        f,
        HandlerSpec(
            (_apply_inbound_presence_filter, ())
        ),
    )
    return f


def outbound_message_filter(f):
    """
    Register the decorated function as a service-level outbound message filter.

    :raise TypeError: if the decorated object is a coroutine function

    .. seealso::

       :class:`StanzaStream`
          for important remarks regarding the use of stanza filters.

    """

    if asyncio.iscoroutinefunction(f):
        raise TypeError(
            "outbound_message_filter must not be a coroutine function"
        )

    add_handler_spec(
        f,
        HandlerSpec(
            (_apply_outbound_message_filter, ())
        ),
    )
    return f


def outbound_presence_filter(f):
    """
    Register the decorated function as a service-level outbound presence
    filter.

    :raise TypeError: if the decorated object is a coroutine function

    .. seealso::

       :class:`StanzaStream`
          for important remarks regarding the use of stanza filters.

    """

    if asyncio.iscoroutinefunction(f):
        raise TypeError(
            "outbound_presence_filter must not be a coroutine function"
        )

    add_handler_spec(
        f,
        HandlerSpec(
            (_apply_outbound_presence_filter, ())
        ),
    )
    return f


def _depsignal_spec(class_, signal_name, f, defer):
    signal = getattr(class_, signal_name)

    if isinstance(signal, aioxmpp.callbacks.SyncSignal):
        if not asyncio.iscoroutinefunction(f):
            raise TypeError(
                "a coroutine function is required for this signal"
            )
        if defer:
            raise ValueError(
                "cannot use defer with this signal"
            )
        mode = None
    else:
        if asyncio.iscoroutinefunction(f):
            if defer:
                mode = aioxmpp.callbacks.AdHocSignal.SPAWN_WITH_LOOP(None)
            else:
                raise TypeError(
                    "cannot use coroutine function with this signal"
                    " without defer"
                )
        elif defer:
            mode = aioxmpp.callbacks.AdHocSignal.ASYNC_WITH_LOOP(None)
        else:
            mode = aioxmpp.callbacks.AdHocSignal.STRONG

    if (class_ is not aioxmpp.stream.StanzaStream and
            class_ is not aioxmpp.node.Client):
        deps = (class_,)
    else:
        deps = ()

    return HandlerSpec(
        (
            _apply_connect_depsignal,
            (
                class_,
                signal_name,
                mode,
            )
        ),
        require_deps=deps,
    )


def depsignal(class_, signal_name, *, defer=False):
    """
    Connect the decorated method or coroutine method to the addressed signal on
    a class on which the service depends.

    :param class_: A service class which is listed in the
                   :attr:`~.Meta.ORDER_AFTER` relationship.
    :type class_: :class:`Service` class or one of the special cases below
    :param signal_name: Attribute name of the signal to connect to
    :type signal_name: :class:`str`
    :param defer: Flag indicating whether deferred execution of the decorated
                  method is desired; see below for details.
    :type defer: :class:`bool`

    The signal is discovered by accessing the attribute with the name
    `signal_name` on the given `class_`. In addition, the following arguments are
    supported for `class_`:

    1. :class:`aioxmpp.stream.StanzaStream`: the corresponding signal of the
       stream of the client running the service is used.

    2. :class:`aioxmpp.Client`: the corresponding signal of the client running
       the service is used.

    If the signal is a :class:`.callbacks.Signal` and `defer` is false, the
    decorated object is connected using the default
    :attr:`~.callbacks.AdHocSignal.STRONG` mode.

    If the signal is a :class:`.callbacks.Signal` and `defer` is true and the
    decorated object is a coroutine function, the
    :attr:`~.callbacks.AdHocSignal.SPAWN_WITH_LOOP` mode with the default
    asyncio event loop is used. If the decorated object is not a coroutine
    function, :attr:`~.callbacks.AdHocSignal.ASYNC_WITH_LOOP` is used instead.

    If the signal is a :class:`.callbacks.SyncSignal`, `defer` must be false
    and the decorated object must be a coroutine function.

    .. versionchanged:: 0.9

       Support for :class:`aioxmpp.stream.StanzaStream` and
       :class:`aioxmpp.Client` as `class_` argument was added.
    """

    def decorator(f):
        add_handler_spec(
            f,
            _depsignal_spec(class_, signal_name, f, defer)
        )
        return f
    return decorator


def _depfilter_spec(class_, filter_name):
    require_deps = ()
    if class_ is not aioxmpp.stream.StanzaStream:
        require_deps = (class_,)

    return HandlerSpec(
        (
            _apply_connect_depfilter,
            (
                class_,
                filter_name,
            )
        ),
        is_unique=True,
        require_deps=require_deps,
    )


def depfilter(class_, filter_name):
    """
    Register the decorated method at the addressed :class:`~.callbacks.Filter`
    on a class on which the service depends.

    :param class_: A service class which is listed in the
                   :attr:`~.Meta.ORDER_AFTER` relationship.
    :type class_: :class:`Service` class or
                  :class:`aioxmpp.stream.StanzaStream`
    :param filter_name: Attribute name of the filter to register at
    :type filter_name: :class:`str`

    The filter at which the decorated method is registered is discovered by
    accessing the attribute with the name `filter_name` on the instance of the
    dependent class `class_`. If `class_` is
    :class:`aioxmpp.stream.StanzaStream`, the filter is searched for on the
    stream (and no dependendency needs to be declared).

    .. versionadded:: 0.9
    """
    spec = _depfilter_spec(class_, filter_name)

    def decorator(f):
        add_handler_spec(
            f,
            spec,
        )
        return f

    return decorator


def is_iq_handler(type_, payload_cls, coro):
    """
    Return true if `coro` has been decorated with :func:`iq_handler` for the
    given `type_` and `payload_cls`.
    """

    try:
        handlers = get_magic_attr(coro)
    except AttributeError:
        return False

    return HandlerSpec(
        (_apply_iq_handler, (type_, payload_cls)),
    ) in handlers


def is_message_handler(type_, from_, cb):
    """
    Deprecated alias of :func:`.dispatcher.is_message_handler`.

    .. deprecated:: 0.9
    """
    import aioxmpp.dispatcher
    return aioxmpp.dispatcher.is_message_handler(type_, from_, cb)


def is_presence_handler(type_, from_, cb):
    """
    Deprecated alias of :func:`.dispatcher.is_presence_handler`.

    .. deprecated:: 0.9
    """
    import aioxmpp.dispatcher
    return aioxmpp.dispatcher.is_presence_handler(type_, from_, cb)


def is_inbound_message_filter(cb):
    """
    Return true if `cb` has been decorated with :func:`inbound_message_filter`.
    """

    try:
        handlers = get_magic_attr(cb)
    except AttributeError:
        return False

    return HandlerSpec(
        (_apply_inbound_message_filter, ())
    ) in handlers


def is_inbound_presence_filter(cb):
    """
    Return true if `cb` has been decorated with
    :func:`inbound_presence_filter`.
    """

    try:
        handlers = get_magic_attr(cb)
    except AttributeError:
        return False

    return HandlerSpec(
        (_apply_inbound_presence_filter, ())
    ) in handlers


def is_outbound_message_filter(cb):
    """
    Return true if `cb` has been decorated with
    :func:`outbound_message_filter`.
    """

    try:
        handlers = get_magic_attr(cb)
    except AttributeError:
        return False

    return HandlerSpec(
        (_apply_outbound_message_filter, ())
    ) in handlers


def is_outbound_presence_filter(cb):
    """
    Return true if `cb` has been decorated with
    :func:`outbound_presence_filter`.
    """

    try:
        handlers = get_magic_attr(cb)
    except AttributeError:
        return False

    return HandlerSpec(
        (_apply_outbound_presence_filter, ())
    ) in handlers


def is_depsignal_handler(class_, signal_name, cb, *, defer=False):
    """
    Return true if `cb` has been decorated with :func:`depsignal` for the given
    signal, class and connection mode.
    """
    try:
        handlers = get_magic_attr(cb)
    except AttributeError:
        return False

    return _depsignal_spec(class_, signal_name, cb, defer) in handlers


def is_depfilter_handler(class_, filter_name, filter_):
    """
    Return true if `filter_` has been decorated with :func:`depfilter` for the
    given filter and class.
    """
    try:
        handlers = get_magic_attr(filter_)
    except AttributeError:
        return False

    return _depfilter_spec(class_, filter_name) in handlers
