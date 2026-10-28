"""MV Aurora Atlantic — illustrative cruise ship, 290 m x 32 m, 14 m hull depth.

Authored from public cruise-ship proportions (mid-size Post-Panamax), not a
surveyed vessel. CAD axes: x east (beam), y north (length), z up. The models
check runs this script in the CAD sandbox and stores the validated mesh.
"""
from build123d import *
from northstar_cad import export

LENGTH, BEAM, HULL = 290, 32, 14

# Hull: pointed bow, rounded stern, flat keel; slight sheer omitted (illustrative).
with BuildPart() as hull:
    with BuildSketch(Plane.XY):
        with BuildLine():
            Polyline((-BEAM / 2, -LENGTH / 2 + 12), (-BEAM / 2, LENGTH / 2 - 40), (0, LENGTH / 2),
                     (BEAM / 2, LENGTH / 2 - 40), (BEAM / 2, -LENGTH / 2 + 12))
            ThreePointArc((BEAM / 2, -LENGTH / 2 + 12), (0, -LENGTH / 2), (-BEAM / 2, -LENGTH / 2 + 12))
        make_face()
    extrude(amount=HULL)
    fillet(hull.edges().group_by(Axis.Z)[0], radius=3)

# Stepped white decks with a balcony line, set back from the bow.
decks = None
for level in range(7):
    width = BEAM - 2 - level * 1.2
    length = 210 - level * 14
    part = Pos(0, -18 - level * 2, HULL + 1.5 + level * 3) * Box(width, length, 3)
    decks = part if decks is None else decks + part
decks = fillet(decks.edges().filter_by(Axis.Z), radius=1.5)

bridge = Pos(0, 46, HULL + 13.5) * Box(BEAM + 2, 6, 2.5)
funnel = Pos(0, -60, HULL + 27) * (Cylinder(4.5, 12) - Pos(0, 0, 4) * Cylinder(3.6, 6))
boats = None
for side in (-1, 1):
    for index in range(8):
        boat = Pos(side * (BEAM / 2 - 0.5), -80 + index * 18, HULL + 4) * Box(2.2, 9, 2.2)
        boats = boat if boats is None else boats + boat

export("cruise_ship",
       (hull.part, "#1c3448", "hull"),
       (decks, "#f4f8f9", "decks"),
       (bridge, "#bfe8f0", "bridge"),
       (funnel, "#d9583f", "funnel"),
       (boats, "#f0a23a", "lifeboats"))
