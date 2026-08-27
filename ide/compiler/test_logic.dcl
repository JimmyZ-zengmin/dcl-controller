# Test multi-input AND/OR logic
SENSOR a FROM ADC1_CH0 SCALE 1.0 0.0
SENSOR b FROM ADC1_CH1 SCALE 1.0 0.0
SENSOR c FROM ADC1_CH2 SCALE 1.0 0.0
SENSOR d FROM ADC1_CH3 SCALE 1.0 0.0

# Simple AND chain
LOGIC all_high = a AND b AND c AND d

# Simple OR chain
LOGIC any_high = a OR b OR c OR d

# NOT combined with AND
LOGIC not_a_and_b = NOT a AND b

# Output to GPIO for verification
OUTPUT all_high TO GPIO_PE0
OUTPUT any_high TO GPIO_PE1
OUTPUT not_a_and_b TO GPIO_PE2
