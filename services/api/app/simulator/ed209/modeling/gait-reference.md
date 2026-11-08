# ED-209 Gait Reference (film + motion sources)

How the screen ED-209 walks, mapped to our 6-joint model
(`left/right_{hip_pitch, knee, ankle}`, pitch-only hinges).
Collected 2026-11-04 for the future walking controller / policy.

## The look (Phil Tippett's direction)

- Tippett conceived the movement as **"unanimal"-like, as if constantly
  about to fall over before catching itself** ([Wikipedia](https://en.wikipedia.org/wiki/ED-209)).
- Heavy stompy biped: each step is a forward lurch arrested by the next
  foot slam, with visible side-to-side sway. Slow cadence (~1 step/s),
  wide stance, slight permanent crouch — the Z-legs never fully extend.
- Legs are **reverse-"Z" joints** (animator Randal Dutra, [vfxblog interview](http://vfxblog.com/2017/07/17/robocop-30th-anniversary-stop-motion/)):
  a "chicken walker" with **backwards-facing knees**. The large gun-pod
  arms bob and counter-sway; the hull hunches forward over the steps.
- Full-size hero model: 7 ft (2.1 m), 300–500 lb — our ~2 m / ~100 kg
  scale model matches well.

## Step phases → our joints

| Phase | Hip pitch | Knee | Ankle |
|---|---|---|---|
| Lift | flex forward (swing leg) | fold deep (negative) | curl to clear the foot |
| Swing | continue forward | begin extending | level the foot |
| STOMP | plant | near-straight (never locked) | flat slam, absorb |
| Stance | body vaults over (extends back) | holds slight bend | flat, pushes late |

- Feet slam **flat** (ED-209 has big flat feet, no heel-toe roll).
- The hull pitches/rolls with each catch; arms swing opposite to the legs.

## Motion references (video / rigs)

- `ED-209 Animation` (3ds Max rig + walk cycle):
  https://www.youtube.com/watch?v=eM_ZaTsym6E
- Blender rigged + animated ED-209, **walk cycle and idle included**
  (~$9, Blender 3.1 `.blend` — a candidate for motion retargeting):
  https://superhivemarket.com/products/robocop-rigged-animated
- CGTrader rigged ED-209 with walk cycle + gun/missile animation:
  https://www.cgtrader.com/3d-models/character/sci-fi-character/ed-209-424f0320-c377-4b58-8161-e23022d69488
- Dutra "animating ED-209 in mid-stride" photo (pose reference):
  http://vfxblog.com/2017/07/17/robocop-30th-anniversary-stop-motion/

## Honest limits for our model

- Our legs have **pitch hinges only** — no hip roll/yaw. The screen
  sway is lateral weight shift, which we cannot actuate; a future gait
  must balance passively on the wide feet: short slow steps, CoM kept
  over the stance foot by pitch choreography alone.
- Our skeleton stands straight-legged at STAND while the screen machine
  rests in a Z-fold crouch. A walk-ready rest pose likely wants a slight
  permanent knee bend (a second keyframe), not the straight STAND.
- ED-209 famously cannot do stairs (it falls). Flat arena only — matches
  our shared scene.
