"""Console entry point for Orchestra."""
import asyncio

from orchestra_core.runtime.engine import main


if __name__ == "__main__":
    asyncio.run(main())
