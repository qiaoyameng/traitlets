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

Freezing Traits
^^^^^^^^^^^^^^^

After an object has finished startup, traits that represent configuration can
be marked with the ``freezable=True`` metadata key and then frozen on the
instance. Traits without that metadata remain writable, which is useful for
counters and other runtime state:

.. code:: python

    from traitlets import HasTraits, Int, List, TraitError


    class Service(HasTraits):
        retries = Int(3).tag(freezable=True)
        request_count = Int(0)
        endpoints = List().tag(freezable=True)


    service = Service()
    service.freeze()

    service.retries = 5          # raises TraitError
    service.set_trait("retries", 5)  # also raises TraitError
    service.request_count += 1   # runtime state remains writable

Freezing is instance-specific. It covers direct attribute assignment,
:meth:`HasTraits.set_trait`, and configuration loading. A frozen freezable
trait is rejected before a proposed value can be stored. Allowed updates still
run the usual trait validation and observers, so a temporarily thawed instance
can reject an invalid value.

Use :meth:`HasTraits.temporary_trait_updates` for an explicit, nestable
configuration update block. Each nested block restores the freeze state that
was in effect when that block started, even if the block raises an exception.
The context restores permissions only; it does not roll back values that were
successfully assigned:

.. code:: python

    try:
        with service.temporary_trait_updates():
            service.retries = 4
            with service.temporary_trait_updates():
                service.retries = 5
                raise RuntimeError("update failed")
    except RuntimeError:
        pass

    # The outer block was temporary, so the instance is frozen again.
    # service.retries is still 4; successful assignments are not undone.

Traits added with :meth:`HasTraits.add_traits` use their metadata at the time
of assignment. A freezable trait added while the instance is frozen is frozen
immediately, while a newly added non-freezable trait is writable.

Freezing does not make values deeply immutable. If a freezable trait refers to
a mutable value such as a list or dict, replacing the trait value is blocked,
but in-place operations on that object are not:

.. code:: python

    service.endpoints.append("/health")  # allowed; the object is mutated
    service.endpoints = ["/health"]      # blocked; the trait is reassigned

If a freezable trait has a dynamic default, reading it after freezing still
creates and stores that default. Any writes made by the default generator are
subject to the freeze rules in effect at the time the default is read.

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

Custom Events
-------------

Finally, trait types can emit other events types than trait changes. This
capability was added so as to enable notifications on change of values in
container classes. The items available in the dictionary passed to the observer
registered with ``observe`` depends on the event type.
