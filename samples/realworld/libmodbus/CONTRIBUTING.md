# How Do I Submit A Good Bug Report?

Please, don't send direct emails to Stéphane Raimbault unless you want
commercial support.

Take care to read the documentation at http://libmodbus.org/.

- _Be sure it's a bug before creating an issue_, in doubt, post a message on
  <https://groups.google.com/forum/#!forum/libmodbus> or send an email to
  <libmodbus@googlegroups.com>

- _Use a clear and descriptive title_ for the issue to identify

- _Which version of libmodbus are you using?_ you can obtain this information
  from your package manager or by running `pkg-config --modversion libmodbus`.
  You can provide the sha1 of the commit if you have fetched the code with `git`.

- _Which operating system are you using?_

- _Describe the exact steps which reproduce the problem_ in as many details as
  possible. For example, the software/equipment which runs the Modbus server, how
  the clients are connected (TCP, RTU, ASCII) and the source code you are using.

- _Enable the debug mode_, libmodbus provides a function to display the content
  of the Modbus messages and it's very convenient to analyze issues
  (http://libmodbus.org/docs/modbus_set_debug/).

Good bug reports provide right and quick fixes!

## Contributor License Agreement

By submitting a contribution to libmodbus, you agree to the terms of the
[Contributor License Agreement](CLA.txt). Please read it before submitting a
pull request.
