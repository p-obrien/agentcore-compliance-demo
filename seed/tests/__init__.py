"""Test package for the AgentCore compliance demo seed loader.

Property and unit tests for the seed loader's routing, idempotent upsert, and
bounded-retry logic live here. The OpenSearch HTTP boundary and the clock/sleep
boundary are mocked via fixtures in ``conftest.py`` so tests never make a live
AWS call and never sleep for real.
"""
