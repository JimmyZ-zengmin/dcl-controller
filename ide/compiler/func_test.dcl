# DCL Functional Test Program
# Purpose: Verify compiler + deploy + hardware execution

# Sensor inputs
SENSOR temp FROM ADC1_CH0 SCALE 1.0 0.0

# Logic: Check if WIRE[2] > 0.5 (should be true, WIRE[2] ≈ 1.0)
ARITH temp_high = temp GT 0.5

# Output
OUTPUT temp_high TO GPIO_PE0
