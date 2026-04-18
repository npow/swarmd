"""Temporal workflow-level tests for the durable swarm.

These tests drive ``MissionWorkflow`` and its children through
``temporalio.testing.WorkflowEnvironment`` (time-skipping mode) so we can
verify the verifier loop, signal handlers, and query handlers without a
real Temporal server.
"""
