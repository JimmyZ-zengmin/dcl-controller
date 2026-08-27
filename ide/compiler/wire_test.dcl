# DCL Functional Test - WIRE Value Test
# Purpose: Verify compiler + deploy + hardware execution

# Sensor input
SENSOR temp FROM ADC1_CH0 SCALE 1.0 0.0

# Test EQ: temp == 0.0 → should be TRUE (1.0)
EQ is_zero FROM temp == 0.0

# Test NE: temp != 5.0 → should be TRUE (1.0)
NE not_five FROM temp != 5.0

# Output results
OUTPUT is_zero TO GPIO_PE0
OUTPUT not_five TO GPIO_PE1
