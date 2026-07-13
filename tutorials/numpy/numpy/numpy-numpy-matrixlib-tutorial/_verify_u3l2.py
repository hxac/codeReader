import warnings
warnings.simplefilter("ignore")
import numpy as np

print("numpy version:", np.__version__)
m = np.matrix(np.arange(12).reshape(3, 4))
print("m.shape", m.shape)

print("--- sum (uses _collapse + keepdims=True) ---")
print("sum(axis=0).shape", m.sum(axis=0).shape, type(m.sum(axis=0)).__name__)
print("sum(axis=1).shape", m.sum(axis=1).shape, type(m.sum(axis=1)).__name__)
print("sum() type", type(m.sum()).__name__, "value", m.sum())

print("--- argmax (uses _align, NO keepdims) ---")
print("argmax(axis=0).shape", m.argmax(axis=0).shape, type(m.argmax(axis=0)).__name__)
print("argmax(axis=1).shape", m.argmax(axis=1).shape, type(m.argmax(axis=1)).__name__)
print("argmax() value", m.argmax(), type(m.argmax()).__name__)

print("--- ptp (uses _align) ---")
print("ptp(axis=0).shape", m.ptp(axis=0).shape, type(m.ptp(axis=0)).__name__)
print("ptp(axis=1).shape", m.ptp(axis=1).shape, type(m.ptp(axis=1)).__name__)
print("ptp() value", m.ptp(), type(m.ptp()).__name__)

print("--- intermediate: what ndarray.argmax returns WITHOUT keepdims ---")
base0 = np.ndarray.argmax(m, 0)
print("ndarray.argmax(m,0) -> shape", np.shape(base0), "is matrix:", isinstance(base0, np.matrix))
base1 = np.ndarray.argmax(m, 1)
print("ndarray.argmax(m,1) -> shape", np.shape(base1), "is matrix:", isinstance(base1, np.matrix))

print("--- compare: np.sum vs ndarray.sum keepdims on a 1-D result ---")
print("Does ndarray.argmax accept keepdims? try:")
try:
    r = np.ndarray.argmax(m, 0, keepdims=True)
    print("  yes, shape", np.shape(r))
except TypeError as e:
    print("  no:", e)
