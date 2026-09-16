"""Whether this is a Microsoft Store build.

`make_store_package.py` rewrites the line below to `True` before it builds the
package and back to `False` afterwards - including when a build fails, so the next
portable build is never left refusing its own setup. It is committed as `False`,
which is what a source checkout and the portable release both want.

Why a build-time flag when the program can already detect a package at run time
(`apppaths.is_packaged()`): detection is a call into Windows that could fail or be
unavailable, and the guarantee the Store needs is stronger than "should refuse".
A build that cannot set the sensors up is a fact about the binary, and one a
reviewer can check by running `gpumon.exe --setup-sensors` against the package.
"""

#: True only in the package submitted to the Store.
IS_STORE_BUILD = False

#: What to tell anyone who asks a Store build to set the sensors up.
REFUSAL = (
    "This build is the Microsoft Store package, which cannot install a sensor "
    "driver. The CPU temperature is available in the portable download, whose "
    "setup installs the helper once; this app reads its readings if it is there.")
