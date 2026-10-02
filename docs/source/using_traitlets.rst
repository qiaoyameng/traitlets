Using Traitlets
===============

In short, traitlets let the user define classes that have

1. Attributes (traits) with type checking and dynamically computed
   default values
2. Traits emit change events when attributes are modified
3. Traitlets perform some validation and allow coercion of new trait
   values on assignment. They also allow the user to define custom
   validation logic for attributes based on the value of other
   attributes.

Default values, and checking type and value
-------------------------------------------

At its most basic, traitlets provides type checking, and dynamic default
value generation of attributes on :class:`traitlets.HasTraits`
subclasses:

.. code:: python

    from traitlets import HasTraits, Int, Unicode, default
    import getpass


    class Identity(HasTraits):
        username = Unicode()

        @default("username")
        def _default_username(self):
            return getpass.getuser()

.. code:: python

    class Foo(HasTraits):
        bar = Int()


    foo = Foo(bar="3")  # raises a TraitError

::

    TraitError: The 'bar' trait of a Foo instance must be an int,
    but a value of '3' <class 'str'> was specified

observe
-------

Traitlets implement the observer pattern

.. code:: python

    class Foo(HasTraits):
        bar = Int()
        baz = Unicode()


    foo = Foo()


    def func(change):
        print(change["old"])
        print(change["new"])  # as of traitlets 4.3, one should be able to
        # write print(change.new) instead


    foo.observe(func, names=["bar"])
    foo.bar = 1  # prints '0\n 1'
    foo.baz = "abc"  # prints nothing

When observers are methods of the class, a decorator syntax can be used.

.. code:: python

    class Foo(HasTraits):
        bar = Int()
        baz = Unicode()

        @observe("bar")
        def _observe_bar(self, change):
            print(change["old"])
            print(change["new"])

Validation and Coercion
-----------------------

Custom Cross-Validation
^^^^^^^^^^^^^^^^^^^^^^^

Each trait type (``Int``, ``Unicode``, ``Dict`` etc.) may have its own
validation or coercion logic. In addition, we can register custom
cross-validators that may depend on the state of other attributes.

Basic Example: Validating the Parity of a Trait
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code:: python

    from traitlets import HasTraits, TraitError, Int, Bool, validate


    class Parity(HasTraits):
        data = Int()
        parity = Int()

        @validate("data")
        def _valid_data(self, proposal):
            if proposal["value"] % 2 != self.parity:
                raise TraitError("data and parity should be consistent")
            return proposal["value"]

        @validate("parity")
        def _valid_parity(self, proposal):
            parity = proposal["value"]
            if parity not in [0, 1]:
                raise TraitError("parity should be 0 or 1")
            if self.data % 2 != parity:
                raise TraitError("data and parity should be consistent")
            return proposal["value"]


    parity_check = Parity(data=2)

    # Changing required parity and value together while holding cross validation
    with parity_check.hold_trait_notifications():
        parity_check.data = 1
        parity_check.parity = 1

Notice how all of the examples above return
``proposal['value']``. Returning a value
is necessary for validation to work
properly, since the new value of the trait will be the
return value of the function decorated by ``@validate``. If this
function does not have any ``return`` statement, then the returned
value will be ``None``, instead of what we wanted (which is ``proposal['value']``).

However, we recommend that custom cross-validators don't modify the state of
the HasTraits instance.

Advanced Example: Validating the Schema
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``List`` and ``Dict`` trait types allow the validation of nested
properties.

.. code:: python

    from traitlets import HasTraits, Dict, Bool, Unicode


    class Nested(HasTraits):
        value = Dict(
            per_key_traits={"configuration": Dict(value_trait=Unicode()), "flag": Bool()}
        )


    n = Nested()
    n.value = dict(flag=True, configuration={})  # OK
    n.value = dict(flag=True, configuration="")  # raises a TraitError.


However, for deeply nested properties it might be more appropriate to use an
external validator:

.. code:: python

    import jsonschema

    value_schema = {
        "type": "object",
        "properties": {
            "price": {"type": "number"},
            "name": {"type": "string"},
        },
    }

    from traitlets import HasTraits, Dict, TraitError, validate, default


    class Schema(HasTraits):
        value = Dict()

        @default("value")
        def _default_value(self):
            return dict(name="", price=1)

        @validate("value")
        def _validate_value(self, proposal):
            try:
                jsonschema.validate(proposal["value"], value_schema)
            except jsonschema.ValidationError as e:
                raise TraitError(e)
            return proposal["value"]


    s = Schema()
    s.value = dict(name="", price="1")  # raises a TraitError


Holding Trait Cross-Validation and Notifications
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Sometimes it may be impossible to transition between valid states for a
``HasTraits`` instance by changing attributes one by one. The
``hold_trait_notifications`` context manager can be used to hold the custom
cross validation until the context manager is released. If a validation error
occurs, changes are rolled back to the initial state.

Freezing Traits
---------------

Once a service has finished starting up, it is often desirable to pin its
configuration traits while keeping runtime fields (counters, caches, ...)
writable. A trait opts into this by declaring ``freezable=True`` metadata,
and an instance is then frozen with :meth:`~.HasTraits.freeze`::

    from traitlets import HasTraits, Int, Unicode


    class Service(HasTraits):
        host = Unicode("localhost").tag(freezable=True)
        port = Int(8080).tag(freezable=True)
        requests = Int(0)  # no freezable metadata: stays writable


    service = Service()
    service.freeze()
    service.host = "example.com"  # raises a TraitError
    service.requests += 1  # runtime traits are unaffected

The freeze applies uniformly to every write path: regular attribute
assignment, :meth:`~.HasTraits.set_trait`, and loading values from a config
object (e.g. ``Configurable.update_config``). Freezing never bypasses
validation — it only rejects writes to freezable traits before they happen,
while writable traits keep being validated as usual.
:meth:`~.HasTraits.unfreeze` lifts the freeze permanently, and the
:attr:`~.HasTraits.frozen` property reports the current state.

For scoped updates — for example a maintenance window — use the
:meth:`~.HasTraits.temporarily_unfrozen` context manager. It restores the
previous freeze state on exit, even if the block raises, and it nests::

    with service.temporarily_unfrozen():
        service.host = "maintenance.example.com"
        with service.temporarily_unfrozen():
            service.port = 9090
    assert service.frozen  # restored, even if an exception was raised above

A few things are worth noting about the semantics:

* Traits added later with :meth:`~.HasTraits.add_traits` are governed by
  their own ``freezable`` metadata as soon as the freeze is (back) in effect.
* Reading a trait whose dynamic default has not been computed yet still
  materializes and caches that default while frozen; only explicit writes
  are guarded.
* The freeze is **shallow**: it guards assignment of trait attributes on the
  instance, but it is not a deep immutable container. Mutating a trait's
  value in place (e.g. appending to a ``List`` trait's value, or changing a
  key of a ``Dict``) is still possible. If a value must not be mutated at
  all, use an immutable value such as a ``Tuple``.

Custom Events
-------------

Finally, trait types can emit other events types than trait changes. This
capability was added so as to enable notifications on change of values in
container classes. The items available in the dictionary passed to the observer
registered with ``observe`` depends on the event type.
