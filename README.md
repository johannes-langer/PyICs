# PyICs
---
_Python Interactive Courses_

> This project is managed using ``uv``. If you use ``uv``, you can use the command  
> ``uv run pyics``, or alternatively install the package using ``pip install .``, and then simply run ``pyics``.

## What is PyICs?

PyICs is a framework for interactive Python tutorials, inspired by the _rustlings_ tutorial for the _Rust_ programming language. While PyICs comes with basic courses, it is designed to allow for easy extension with custom courses to address specific topics.

## TODO

- [x] Main CLI, including
  - [x] A progress bar
  - [x] A hotkey reference
- [x] ``PyICs`` main class as controller
- [x] Logic for evaluating a file when it is saved.
- [ ] Implement Test-Framework. Do these tests:
  - [ ] ``PrintTest``: Printout of File needs to be correct
  - [ ] ``SimpleTest``: Output of ``func`` needs to match for some inputs
  - [ ] ``StructureTest``: File contains some code-snippet (as naming invariant as possible)
  - [ ] ``ProtocolTest``: Symbols in file match some protocol
- [ ] Save Tasks as json files. When simply launching pyics, it should check the working directory for a ``.tasks`` directory, assuming that task json files are stored there.
- [ ] Pick some library for high-quality error messages
- [ ] Track Progress (simply store a json file). Only prompt for a username on first launch or reset.
- [ ] Links in console output, so that I can click on the current exercise file, just like in Rustlings
- [ ] Tasks should be able to include hints, which can be shown in the info-box of the header
