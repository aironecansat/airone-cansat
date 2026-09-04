"""End-to-end: simulated packets -> real pipeline -> scientific worker -> DB.

This exercises the full Tier-1 -> Tier-2 path: binary frames produced by the
simulator are parsed and pushed through the 20-stage pipeline, whose stage-17
sink feeds the scientific worker, which runs the analysis registry and persists
valid results through the AnalysisRepository.
"""
from __future__ import annotations

import queue
import time

from src.data_processing.pipeline import PipelineContext, ProcessingPipeline
from src.simulation import FlightProfile, MissionSimulator, SimulatedPacketSource
from src.storage.database import DatabaseManager
from src.storage.repositories import AnalysisRepository
from src.telemetry.parser import StreamParser
from src.workers.packet_processor import decode_payload_to_frame
from src.workers.scientific_worker import ScientificAnalysisWorker


def _db(temp_db_path) -> DatabaseManager:
    db = DatabaseManager(temp_db_path)
    db.run_migrations()
    return db


def test_pipeline_feeds_scientific_worker_and_persists(temp_db_path):
    db = _db(temp_db_path)
    repo = AnalysisRepository(db)
    sci_q: "queue.Queue" = queue.Queue()

    ctx = PipelineContext(scientific_sink=lambda f: sci_q.put(f))
    pipeline = ProcessingPipeline(ctx)

    sim = MissionSimulator(profile=FlightProfile(apogee_m=1000.0), sample_rate_hz=2.0, seed=11)
    packets = SimulatedPacketSource(sim).collect()

    parser = StreamParser()
    processed = 0
    for pkt in packets:
        for parsed in parser.parse_stream(pkt):
            if not parsed.crc_valid:
                continue
            frame = decode_payload_to_frame(parsed)
            pipeline.process(frame)
            processed += 1

    assert processed > 100
    assert sci_q.qsize() == processed  # every frame reached the science sink

    # Run the worker's analysis pass directly over the drained frames.
    worker = ScientificAnalysisWorker(
        sci_q, analysis_repo=repo, window_size=10_000, persist=True,
    )
    # Drain the queue into the worker buffer.
    while not sci_q.empty():
        worker._buffer.append(sci_q.get_nowait())
    results = worker.run_now()

    # Several analyses should be valid on a full simulated flight.
    valid = [k for k, v in results.items() if v.get("valid")]
    assert "temperature_gradient" in valid
    assert "atmospheric_density" in valid

    # Valid results must have been persisted.
    stored = repo.get_all(mission_id="default")
    assert len(stored) > 0
    names = {row["analysis_name"] for row in stored}
    assert "temperature_gradient" in names


def test_worker_start_stop_lifecycle(temp_db_path):
    db = _db(temp_db_path)
    repo = AnalysisRepository(db)
    sci_q: "queue.Queue" = queue.Queue()
    sim = MissionSimulator(profile=FlightProfile(apogee_m=500.0), sample_rate_hz=4.0, seed=2)
    for fr in sim.collect():
        sci_q.put(fr)

    worker = ScientificAnalysisWorker(sci_q, analysis_repo=repo, run_interval_s=0.2, window_size=10_000)
    worker.start()
    time.sleep(1.0)
    assert worker.is_alive()
    narrative = worker.latest_narrative()
    assert worker.stop(timeout=5.0)
    # Narrative must contain the honesty disclaimer.
    assert "disclaimer" in narrative
    assert "causation" in narrative["disclaimer"].lower()
