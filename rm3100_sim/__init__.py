"""A register-level RM3100 simulator, driven by a physical field model.

The simulator answers I2C transfers the way the chip does (``device``), serves
them over TCP (``server``), and fills the measurement registers from a field
model (``physics``) configured by a scenario file (``scenario``): the World
Magnetic Model for a site, the solar-quiet daily variation, storms, local
disturbances, UAP passes modelled as magnetic dipoles, sensor noise from the
datasheet, and I2C faults. Everything random is derived from one seed.
"""
