# HeyWalk Docker workflow.
#
# Every build first destroys running/stale project containers, so an image
# build never runs against (or collides with) an old stack:
#   make build    # down, then build web + api + runner images
#   make rebuild  # down, then clean build without cache
#   make up       # start the full stack (open http://localhost:8021)
#   make down     # stop and remove project containers
#   make runner   # build only the isolated runner image (ROS 2 Jazzy + MuJoCo)
#
# The runner image is REQUIRED to execute experiments: it is the isolation
# boundary and it bundles the MuJoCo physics engine used by the default
# simulation backend. `build` and `up` both produce it.
#
# `down` preserves the heywalk-data volume (SQLite experiment history).
# To also wipe experiment data: docker compose down --volumes

.PHONY: build rebuild up down runner

down:
	docker compose down --remove-orphans

# Profile-gated: the runner is a build-only service, never started by `up`.
runner:
	docker compose --profile runner-build build runner

build: down
	docker compose build
	docker compose --profile runner-build build runner

rebuild: down
	docker compose build --no-cache
	docker compose --profile runner-build build --no-cache runner

up:
	mkdir -p /tmp/heywalk-staging && chmod 777 /tmp/heywalk-staging
	docker compose --profile runner-build build runner
	docker compose up --build
