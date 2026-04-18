"""Tests for swarm.durable.activities.

Each activity lives in its own module in ``swarm/durable/activities/`` and
has a matching test file here. Tests use ``temporalio.testing.ActivityEnvironment``
so we can exercise the ``@activity.defn`` coroutine without spinning up a
full Temporal worker.
"""
