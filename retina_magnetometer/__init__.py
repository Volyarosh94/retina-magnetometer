"""RM3100 magnetometer app for RETINA nodes.

The package is the node-side half of the repository: the RM3100 driver, the
sampler that drives it, bounded local storage, orientation from the Earth's
field, health reporting and the browser UI. The simulator in ``rm3100_sim``
drives this package through the same I2C interface a real sensor does, and
nothing here imports it.
"""
