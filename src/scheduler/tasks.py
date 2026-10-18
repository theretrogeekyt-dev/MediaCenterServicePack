import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import settings
from src.epg.pipeline import EPGPipeline

logger = logging.getLogger(__name__)


class SchedulerService:
    """Manages periodic cron execution for EPG synchronization."""

    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone=settings.TZ)
        self.epg_pipeline = EPGPipeline()
        self._is_running = False

    def start(self):
        """Register cron jobs and start the background scheduler."""
        if self._is_running:
            return

        # 1. EPG Sync Job
        try:
            epg_trigger = CronTrigger.from_crontab(settings.CRON_EPG_SYNC, timezone=settings.TZ)
            self.scheduler.add_job(
                self.run_epg_sync,
                trigger=epg_trigger,
                id="epg_sync_job",
                name="EPG Synchronization",
                replace_existing=True,
            )
            logger.info(f"Registered EPG sync job with cron: '{settings.CRON_EPG_SYNC}' (TZ: {settings.TZ})")
        except Exception as e:
            logger.error(f"Failed to parse CRON_EPG_SYNC ('{settings.CRON_EPG_SYNC}'): {e}")

        self.scheduler.start()
        self._is_running = True
        logger.info("Background scheduler successfully started.")

    def shutdown(self):
        """Stop the background scheduler."""
        if self._is_running:
            self.scheduler.shutdown(wait=False)
            self._is_running = False
            logger.info("Background scheduler stopped.")

    async def run_epg_sync(self, source_url: Optional[str] = None):
        """Execute the EPG synchronization pipeline."""
        logger.info("Scheduler triggering EPG synchronization...")
        try:
            await self.epg_pipeline.run(source_url=source_url)
        except Exception as e:
            logger.error(f"Scheduled EPG sync failed: {e}")

    def get_job_info(self) -> Dict[str, Any]:
        """Retrieve execution schedules and next run times."""
        jobs_data = {}
        for job in self.scheduler.get_jobs():
            jobs_data[job.id] = {
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            }
        return jobs_data


# Global scheduler singleton
scheduler_service = SchedulerService()
