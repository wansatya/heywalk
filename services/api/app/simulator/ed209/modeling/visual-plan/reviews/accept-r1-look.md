# Accept round 1 — look review (front34)
tone_compare: background ref 227.00 vs render 255.00 (+28.00); box_w 0.69 vs 0.49 (-0.20); box_h 0.76 vs 0.65 (-0.12); mean 115.03 vs 138.03 (+23.00); contrast 50.15 vs 35.00 (-15.15); dark5 22.08 vs 96.00 (+73.93); bright5 184.10 vs 206.94 (+22.84)

- [major] armor reads as mirror chrome with blown streak highlights vs matte painted blue-gray -> roughness ~0.6-0.75 metallic ~0.1 base #B7C3C8
- [major] armor hue neutral silver ~#C8C8C8 vs cool blue-gray ~#B7C3C8 -> tint base color blue-gray
- [major] dark joints/gaps mid-gray ~#8A8A8A with dark5 96.0 vs near-black ~#1A1A1A with dark5 22.1 (delta +73.9) -> darken joint cavities/base to reach dark5 ~22
- [major] specular highlights blown to white (bright5 206.9 vs 184.1, delta +22.8) with hard streaks vs soft diffused sheen -> kill chrome, raise roughness, smaller/weaker key
- [major] contrast 35.0 vs 50.2 (delta -15.2), subject looks flat-washed vs punchy modeled form -> stronger key / less fill
- [major] mean luminance 138.0 vs 115.0 (delta +23.0), whole subject too bright -> lower key/ambient energy or exposure
- [major] red/yellow/green cable accents missing, cables render silver-gray vs red + yellow + black -> add colored cable materials
- [major] red marker decals/stripes on hull missing entirely vs visible red accents -> add red decal material
- [major] top canopy flat dark-gray slab vs textured black fabric/pad -> matte near-black rough textured material
- [major] camera shows rear of machine vs front-3/4 view with guns facing camera -> move camera to front 3/4
- [major] background pure white 255 vs light studio gray 227 (delta +28) -> background #E3E3E3ish (227)
- [moderate] framing subject 71% of reference width (box_w 0.49 vs 0.69) and 86% of height -> move camera closer / longer focal length
- [moderate] ground shadow small hard blob left of feet vs broad soft shadow under/behind feet -> larger softer shadow, light from front-right-top
- [moderate] vent grilles/gun-barrel muzzles unreadable chrome mush vs crisp dark recessed detail -> dark non-metal vent/barrel material + AO
- [moderate] panel weathering/edge wear absent, surfaces uniform vs subtle scuffed worn paint -> add light edge-wear roughness variation
- [moderate] foot/leg form reads melted-smooth with smeared highlights vs crisp hard-surface plates -> harden normals/edges, lower roughness variance
- [minor] foot toe caps face camera as single dome vs split left/right toes with dark sole line -> separate toe meshes with dark gap
