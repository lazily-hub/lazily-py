## 0.9.0

* Add type-aware context to slot and cell functions.
* The ctx argument type can be enforced via mypy.
* mypy tests now pass

## 0.8.0

* Add Cell.get() as an alias to Cell.value
* Add Cell.set(value) as an alias to Cell.value = value

## 0.7.0

* Add @slot_def and @cell_def decorators...which both take a resolve_ctx callable to resolve the ctx object.
* Allows for calling a slot function with an object that contains the ctx, such as a Request or Graphene
  GraphQLResolveInfo.

## 0.6.0

* cell() is supported with a default value of None.

## 0.5.0

* Slot now support subscriptions. Resetting a slot will also reset it's descendents.

## 0.4.0

* BREAKING CHANGE: Rename Cell to Slot and @cell to @slot.
* BREAKING CHANGE: Cell is now a subscribable that resets a parent Slots when it changes.

## 0.3.0

* BREAKING CHANGE: Rename be to cell and Be to Cell.

## 0.2.0

* BREAKING CHANGE: Remove be_class due to complexity. If a subclass of Be is needed, then users are encouraged to use
  create a singleton instance of the subclass.

## 0.1.0

* Initial release