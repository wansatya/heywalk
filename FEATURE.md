# Robot Foundry — Feature Specification

**Product:** HeyWalk Robotics IDE  
**Feature:** Robot Foundry  
**Status:** Proposed  
**Version:** 1.0  
**Type:** New feature integrated into existing IDE

---

## 1. Overview

Robot Foundry is a new feature that allows users to create simulation-ready robot models from visual references.

Users upload a robot image. The system analyzes the robot, identifies its components, reconstructs individual 3D parts, estimates mechanical structure, generates a MuJoCo MJCF model, and prepares the robot for simulation.

The goal is to make robot creation accessible to people who don't have traditional CAD, mechanical engineering, or robotics modeling experience.

### Product promise

**From robot image to simulation-ready robot.**

### Core workflow

```text
Robot Image
    |
    v
AI Detection & Segmentation
    |
    v
Robot Part Extraction
    |
    v
3D Reconstruction
    |
    v
Mechanical Assembly
    |
    v
Physics Configuration
    |
    v
MJCF Generation
    |
    v
MuJoCo Validation
    |
    v
Robot Simulation
```

## 2. Objectives

1. Allow users to create a robot model from one or more images.
2. Automatically identify and segment robot components.
3. Generate 3D assets for individual robot parts.
4. Build an articulated robot hierarchy.
5. Generate MuJoCo-compatible MJCF.
6. Estimate physical properties with explicit confidence levels.
7. Validate the generated model before simulation.
8. Allow users to edit and regenerate individual components.
9. Integrate generated robots directly into the existing IDE.

### Non-goals

- Automatically manufacturing a physically accurate robot.
- Guaranteeing that image-derived geometry represents actual internal mechanical structures.
- Generating production-ready CAD or manufacturing drawings.
- Automatically generating a trained walking policy.
- Guaranteeing compatibility with arbitrary pretrained ONNX policies.

## 3. User Experience

### 3.1 IDE Navigation

Add a new navigation item:

**Robot Foundry**

Suggested navigation:

```text
HeyWalk IDE
├── Dashboard
├── Projects
├── Robot Foundry       [NEW]
├── Simulator
├── Code Editor
├── Terminal
└── Settings
```

Robot Foundry should be accessible without leaving the existing IDE.

### 3.2 Foundry Dashboard

Display previously generated robot projects.

Each robot card should contain:

- Robot thumbnail.
- Robot name.
- Generation status.
- Number of identified components.
- Number of articulated joints.
- MJCF validation status.
- Last modified timestamp.

Actions:

- Open Foundry project.
- Open in Simulator.
- Edit robot.
- Export package.
- Delete project.

Include a prominent **Create New Robot** action.

### 3.3 Create Robot

The creation screen should support:

**Input methods**

- Upload robot image (PNG, JPG, WEBP).
- Upload multiple reference images.
- Optional robot description.
- Optional robot category.

Example:

```text
Robot name: ED-209

Description:
A large armored security robot with two
mechanical legs, upper body, weapon arms
and an articulated head.

Reference images:
[ Upload Images ]

[ Analyze Robot ]
```

For the initial MVP, support image input first. Multiple views should be encouraged because a single image cannot reliably reconstruct hidden geometry.

## 4. Robot Generation Pipeline

### Stage 1: Visual Analysis

**Technology: Meta SAM 3.1**

Use SAM 3.1 for object detection and segmentation.

Tasks:

- Detect the complete robot.
- Identify visible mechanical components.
- Generate segmentation masks.
- Extract component bounding boxes.
- Associate components with semantic labels.
- Identify possible articulation points.

Example output:

```json
{
  "robot": "ED-209",
  "components": [
    {
      "id": "body",
      "label": "main_body",
      "type": "torso",
      "confidence": 0.98
    },
    {
      "id": "left_leg",
      "label": "left_leg",
      "type": "limb",
      "confidence": 0.94
    },
    {
      "id": "right_leg",
      "label": "right_leg",
      "type": "limb",
      "confidence": 0.95
    }
  ]
}
```

Store original images, masks and analysis metadata.

Important: SAM 3.1 provides segmentation and tracking capabilities, not 3D reconstruction or STL generation. The next stages require separate reconstruction and geometry-generation services.

### Stage 2: Component Extraction

Generate isolated images for each detected component.

Requirements:

- Preserve original proportions.
- Preserve component boundaries.
- Remove unwanted background.
- Maintain component identity.
- Store segmentation masks.
- Allow manual mask correction.

Users must be able to:

- Rename components.
- Merge components.
- Split components.
- Remove incorrect detections.
- Add missing components.

### Stage 3: 3D Reconstruction

Convert extracted components into 3D geometry.

Possible technology options:

- Image-to-3D reconstruction models.
- Multi-view reconstruction.
- Procedural geometry generation.
- Mesh processing and repair tools.

Output formats:

- STL
- OBJ
- GLB (optional preview format)

Each component should have:

```text
component/
├── source.png
├── mask.png
├── visual.obj
├── collision.stl
└── metadata.json
```

Metadata example:

```json
{
  "component_id": "left_leg",
  "dimensions": [0.3, 0.2, 0.65],
  "unit": "meter",
  "origin": [0, 0, 0],
  "reconstruction_method": "image_to_3d",
  "confidence": 0.78
}
```

Do not assume a single image provides accurate depth, scale or hidden surfaces. Users should be able to adjust dimensions and review the reconstructed mesh.

### Stage 4: Mechanical Assembly

Construct the robot's articulated structure.

Generate a hierarchical body model.

Example:

```text
Robot
└── Base
    └── Torso
        ├── Head
        │   └── Head Joint
        ├── Left Arm
        │   └── Left Shoulder
        ├── Right Arm
        │   └── Right Shoulder
        ├── Left Leg
        │   ├── Hip
        │   └── Knee
        └── Right Leg
            ├── Hip
            └── Knee
```

For each component, define:

- Parent body.
- Child body.
- Joint type.
- Joint position.
- Joint axis.
- Joint limits.
- Local transformation.
- Fixed or articulated connection.

Supported joint types:

| Joint | Description |
|---|---|
| Fixed | No movement |
| Hinge | Rotational movement |
| Slide | Linear movement |
| Ball | 3-axis rotational movement |
| Free | 6 degrees of freedom |

Provide a visual joint editor.

Users must be able to reposition joints, change joint types and adjust movement limits.

### Stage 5: Physics Configuration

Generate physical properties for every rigid body.

Required properties:

- Mass.
- Center of mass.
- Inertia tensor.
- Collision geometry.
- Friction.
- Contact properties.

Allow three configuration modes:

**Estimated**

AI estimates physical properties using geometry and user-provided assumptions.

**Manual**

Users enter physical values.

**Imported**

Users provide physical properties from an existing CAD or URDF model.

Important: estimated values must be clearly identified as estimates, not measured physical properties.

### Stage 6: Actuator Configuration

Configure motors and control interfaces.

Each actuator should support:

```json
{
  "name": "left_hip_motor",
  "joint": "left_hip",
  "type": "position",
  "gear": 1,
  "ctrlrange": [-1, 1],
  "forcerange": [-100, 100]
}
```

Allow configuration of:

- Actuator type.
- Joint assignment.
- Gear ratio.
- Maximum force or torque.
- Control range.
- Velocity limits where applicable.

Provide sensible defaults but require user review.

### Stage 7: MJCF Generation

Generate a complete MuJoCo XML model.

Main output:

```text
robot.xml
```

The generated MJCF should include:

- Compiler configuration.
- Simulation options.
- Assets.
- World body.
- Robot body hierarchy.
- Inertial definitions.
- Visual geometry.
- Collision geometry.
- Joints.
- Actuators.
- Sensors.
- Default configurations.

The generator must ensure that all mesh references resolve correctly.

Support reusable MJCF includes where appropriate.

### Stage 8: Validation

Before opening the simulator, run automated checks.

Validation checklist:

- XML syntax is valid.
- All mesh references exist.
- All mesh files are readable.
- All articulated bodies have valid inertial properties.
- Joint definitions are valid.
- Joint limits are valid.
- Actuator references exist.
- Collision geometry is available.
- Robot does not have invalid body configurations.
- MuJoCo can compile the model.

Output:

```json
{
  "status": "warning",
  "checks": {
    "xml_valid": true,
    "mesh_files_valid": true,
    "joint_configuration": true,
    "inertia_configuration": false,
    "actuator_configuration": true,
    "mujoco_compilation": true
  },
  "warnings": [
    "Mass and inertia values are estimated."
  ]
}
```

A model that compiles successfully is not necessarily physically accurate or dynamically stable. Keep those validation results separate.

## 5. Foundry Workspace UI

The main workspace should have three areas.

### Left panel: Robot Structure

```text
Robot Structure

ED-209
├── Torso
├── Head
├── Left Arm
├── Right Arm
├── Left Leg
└── Right Leg
```

Features:

- Component selection.
- Rename.
- Add component.
- Remove component.
- Change parent.
- Joint configuration.

### Center panel: 3D Viewport

Display the reconstructed robot.

Required interactions:

- Rotate camera.
- Zoom.
- Pan.
- Select component.
- Hide/show component.
- Toggle wireframe.
- Toggle collision geometry.
- Toggle joint visualization.
- Reset camera.

Use Three.js or an existing 3D viewport already available in the IDE.

### Right panel: Properties

Context-sensitive properties.

For a selected component:

```text
Component: Left Leg

Dimensions
X: 0.30 m
Y: 0.20 m
Z: 0.65 m

Physical Properties
Mass: 5.0 kg
Friction: 0.8

Joint
Type: Hinge
Axis: X
Minimum: -45°
Maximum: 45°

[ Apply Changes ]
```

The UI must distinguish estimated values from user-confirmed values.

## 6. Simulation Integration

Robot Foundry must integrate directly with the existing MuJoCo simulator.

Add an action:

**Open in Simulator**

Workflow:

1. Save the current Foundry project.
2. Generate the complete robot package.
3. Run validation.
4. Compile the MJCF model.
5. Load the robot into the existing simulator.
6. Display the robot in the simulation environment.

Provide simulation presets:

- Empty environment.
- Flat ground.
- Obstacle course.
- Standing test.
- Joint movement test.

Initial simulation capabilities:

- Spawn robot.
- Reset simulation.
- Pause/resume.
- Change simulation speed.
- Inspect joint positions.
- Inspect contact forces.
- Apply manual joint controls.

The simulator should use the generated MJCF directly rather than requiring a separate robot conversion.

## 7. Project Export

Users should be able to download a complete robot package.

Expected structure:

```text
ed209/
├── robot.xml
├── README.md
├── manifest.json
│
├── assets/
│   ├── visual/
│   │   ├── body.obj
│   │   ├── head.obj
│   │   └── ...
│   │
│   └── collision/
│       ├── body.stl
│       ├── head.stl
│       └── ...
│
├── config/
│   ├── physics.json
│   ├── joints.json
│   └── actuators.json
│
├── scenes/
│   └── test_world.xml
│
├── controllers/
│   └── controller.py
│
└── validation/
    └── validation.json
```

The controller is optional and should not be represented as a trained policy unless one actually exists.

## 8. Technical Architecture

Robot Foundry should reuse the existing IDE infrastructure.

Suggested architecture:

```text
Existing HeyWalk IDE
        |
        v
Robot Foundry Frontend
        |
        v
Robot Foundry API
        |
        v
Generation Orchestrator
        |
        +------------------+
        |                  |
        v                  v
   SAM 3.1 API         Image-to-3D
        |                  |
        v                  v
  Segmentation        Mesh Generation
        |                  |
        +---------+--------+
                  |
                  v
          Geometry Processor
                  |
                  v
          Assembly Generator
                  |
                  v
          Physics Generator
                  |
                  v
           MJCF Generator
                  |
                  v
          MuJoCo Validator
                  |
                  v
          Existing Simulator
```

### Backend responsibilities

- Manage generation jobs.
- Store uploaded images.
- Store segmentation masks.
- Manage generated meshes.
- Maintain robot component metadata.
- Generate MJCF.
- Execute model validation.
- Maintain project versions.
- Provide export functionality.

### Suggested API

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/foundry/projects` | Create project |
| GET | `/api/foundry/projects` | List projects |
| GET | `/api/foundry/projects/:id` | Project details |
| POST | `/api/foundry/projects/:id/images` | Upload references |
| POST | `/api/foundry/projects/:id/analyze` | Start segmentation |
| GET | `/api/foundry/jobs/:id` | Check generation status |
| GET | `/api/foundry/projects/:id/components` | List components |
| PATCH | `/api/foundry/components/:id` | Edit component |
| POST | `/api/foundry/projects/:id/reconstruct` | Generate 3D geometry |
| POST | `/api/foundry/projects/:id/assemble` | Generate assembly |
| POST | `/api/foundry/projects/:id/generate-mjcf` | Generate MJCF |
| POST | `/api/foundry/projects/:id/validate` | Validate robot |
| POST | `/api/foundry/projects/:id/simulate` | Launch simulation |
| GET | `/api/foundry/projects/:id/export` | Export package |

Generation endpoints should return job IDs and run long-running processing asynchronously.

## 9. Suggested Technology Stack

Reuse existing IDE libraries and infrastructure wherever possible.

| Layer | Technology |
|---|---|
| Frontend | Existing Vite + React |
| Styling | Existing Tailwind CSS |
| Icons | Existing icon library |
| 3D Viewport | Three.js |
| Segmentation | Meta SAM 3.1 |
| 3D Reconstruction | Image-to-3D model service |
| Mesh Processing | Python, trimesh |
| MJCF Generation | Python |
| Physics Engine | MuJoCo |
| Backend | Existing API infrastructure |
| Job Processing | Background worker |
| Storage | Existing project storage |
| Database | Existing database |

Do not introduce a second authentication system or duplicate project management.

## 10. Database Design

Suggested entities:

### foundry_projects

```text
id
user_id
name
description
status
thumbnail_url
created_at
updated_at
```

### foundry_images

```text
id
project_id
file_url
image_type
analysis_status
created_at
```

### robot_components

```text
id
project_id
parent_id
name
component_type
mesh_url
collision_mesh_url
metadata_json
confidence
created_at
updated_at
```

### robot_joints

```text
id
project_id
component_id
parent_component_id
joint_type
axis
position
limits
metadata_json
```

### robot_actuators

```text
id
project_id
joint_id
actuator_type
configuration_json
```

### foundry_jobs

```text
id
project_id
job_type
status
progress
error_message
result_json
created_at
completed_at
```

### robot_versions

```text
id
project_id
version
mjcf_url
manifest_url
validation_json
created_at
```

Use the existing database conventions where available.

## 11. Generation Status

A project should move through these states:

```text
DRAFT
  |
UPLOADING
  |
ANALYZING
  |
SEGMENTING
  |
RECONSTRUCTING
  |
ASSEMBLING
  |
CONFIGURING_PHYSICS
  |
GENERATING_MJCF
  |
VALIDATING
  |
READY
```

Additional states:

- NEEDS_REVIEW
- FAILED
- CANCELLED

Users must be able to resume editing after a failed stage without restarting the entire pipeline.

## 12. MVP Scope

Do not attempt to implement the entire manufacturing pipeline in the first release.

### Phase 1 — Robot Model Generator

Deliver:

- Robot Foundry navigation.
- Image upload.
- SAM 3.1 segmentation.
- Component detection.
- Manual component editing.
- Initial mesh generation.
- 3D viewport.
- Basic MJCF generation.
- MuJoCo compilation and validation.
- Export robot package.

For MVP, support a simplified robot with a manually reviewable articulated hierarchy.

### Phase 2 — Physics & Assembly

Deliver:

- Joint editor.
- Parent-child hierarchy.
- Physical properties editor.
- Collision mesh generation.
- Actuator configuration.
- Improved model validation.
- Standing and joint movement tests.

### Phase 3 — AI Robotics

Deliver:

- AI-assisted assembly.
- Natural-language robot modifications.
- Automatic joint configuration suggestions.
- Controller generation.
- ONNX policy integration.
- Robot-specific observation/action mapping.
- Simulation diagnostics and iterative correction.

## 13. Acceptance Criteria

The feature is considered MVP-complete when:

1. Users can create a Foundry project.
2. Users can upload a robot reference image.
3. SAM 3.1 can identify and segment visible robot components.
4. Users can inspect and modify component segmentation.
5. The system can generate usable 3D mesh assets.
6. Users can inspect the assembled robot in a 3D viewport.
7. The system can generate a valid MJCF file.
8. MuJoCo can successfully compile the generated model.
9. Users can open the generated robot in the existing simulator.
10. Users can export the complete robot package.
11. Failed jobs provide understandable errors.
12. Generated models preserve their source and version history.

## 14. Important Engineering Constraints

- Never assume SAM segmentation automatically produces 3D geometry.
- Never use visual meshes as the only collision geometry for complex robots.
- Never generate arbitrary inertia values without marking them as estimates.
- Never assume a visible connection represents an actual mechanical joint.
- Never silently discard missing or invalid mesh references.
- Validate MJCF using the actual MuJoCo engine.
- Keep generated files versioned.
- Preserve the original uploaded references.
- Do not execute arbitrary user-generated Python code inside the API server.
- Run reconstruction and simulation workloads in isolated workers or containers.
- Respect third-party model, asset and reconstruction licenses.

---

## 15. Definition of Success

The user should be able to:

1. Upload a picture of a robot.
2. See its individual components identified.
3. Review its reconstructed 3D structure.
4. Adjust joints and physical properties.
5. Generate a MuJoCo model.
6. Open it in the existing simulator.
7. Start experimenting with robot control.

**The ultimate vision:**

A developer should not need to spend days learning CAD and MJCF just to experiment with a robot idea.

Robot Foundry should make robot creation feel as accessible as vibe coding software.