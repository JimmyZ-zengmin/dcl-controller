# Simple test - just read sensor
SENSOR temp FROM ADC1_CH0 SCALE 1.0 0.0
OUTPUT temp TO GPIO_PE0
