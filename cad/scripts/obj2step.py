"""Mesh → solid STEP conversion (docs/08 §4).

**This file runs under FreeCAD's own Python interpreter, not klm's virtualenv.**
It can `import Mesh, Part`; it cannot import anything from klm or from klm's
dependencies, and it must not try. Its entire contract is:

    freecadcmd obj2step.py <source-mesh> <destination.step> [tolerance-mm]

argv in, file out, exit code. Exit 0 on success, non-zero with a message on
stderr otherwise. Nothing is printed to stdout that the caller must parse —
FreeCAD writes its own banner there and it is not worth fighting.

Kept as a file rather than a string inside klm so that it can be run by hand
when a conversion misbehaves, which is the only practical way to debug a
geometry kernel.
"""

import sys

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAILED = 3

DEFAULT_TOLERANCE = 0.1


def convert(source, destination, tolerance=DEFAULT_TOLERANCE):
    import Mesh  # noqa: PLC0415 - only importable inside FreeCAD
    import Part  # noqa: PLC0415

    mesh = Mesh.Mesh(source)
    if mesh.CountFacets == 0:
        sys.stderr.write("obj2step: the mesh has no facets\n")
        return EXIT_FAILED

    shape = Part.Shape()
    shape.makeShapeFromMesh(mesh.Topology, tolerance)

    # A mesh with holes yields a shell that KiCad renders happily and every
    # mechanical tool rejects. Report it rather than exporting silently: the
    # QA gate can only warn about what it is told.
    try:
        solid = Part.makeSolid(shape)
    except Exception as exc:  # noqa: BLE001 - FreeCAD raises bare exceptions
        sys.stderr.write("obj2step: could not make a solid (%s)\n" % exc)
        return EXIT_FAILED

    if not solid.isClosed():
        sys.stderr.write("obj2step: warning: the solid is not watertight\n")

    solid.exportStep(destination)
    return EXIT_OK


def main(argv):
    if len(argv) < 3:
        sys.stderr.write("usage: obj2step.py <source-mesh> <destination.step> [tolerance]\n")
        return EXIT_USAGE
    tolerance = float(argv[3]) if len(argv) > 3 else DEFAULT_TOLERANCE
    try:
        return convert(argv[1], argv[2], tolerance)
    except Exception as exc:  # noqa: BLE001 - the exit code is the contract
        sys.stderr.write("obj2step: %s\n" % exc)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
