# SPDX-License-Identifier: GPL-3.0-or-later
"""Assemble un .ico à partir de PNG (format PNG embarqué, accepté depuis Windows Vista)."""
import struct
import sys


def main(sortie, *pngs):
    images = [open(p, "rb").read() for p in pngs]
    tailles = [struct.unpack(">II", d[16:24]) for d in images]
    entete = struct.pack("<HHH", 0, 1, len(images))
    decalage = 6 + 16 * len(images)
    entrees = b""
    for (l, h), d in zip(tailles, images):
        entrees += struct.pack("<BBBBHHII", l % 256, h % 256, 0, 0, 1, 32, len(d), decalage)
        decalage += len(d)
    with open(sortie, "wb") as f:
        f.write(entete + entrees + b"".join(images))


if __name__ == "__main__":
    main(sys.argv[1], *sys.argv[2:])
