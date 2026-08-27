# Test with different SCALE to verify computation
# If ADC1_CH0 has some value, scaling it should change the result
SENSOR temp FROM ADC1_CH0 SCALE 2.0 10.0
OUTPUT temp TO GPIO_PE0
