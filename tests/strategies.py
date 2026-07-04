from hypothesis import strategies as st

# t in [0, 1] — the domain of gradient/lerp parameters
unit_interval = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# An RGB channel triple, each 0-255
rgb_tuple = st.tuples(
    st.integers(min_value=0, max_value=255),
    st.integers(min_value=0, max_value=255),
    st.integers(min_value=0, max_value=255),
)

# Hue/sat/val each in [0, 1]
unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
