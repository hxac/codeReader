import numpy as np
from scipy.differentiate import derivative

calls = []
def f(x):
    x = np.asarray(x)
    calls.append(np.sort(np.unique(np.round(x, 10))))
    return np.exp(x)

# default order=8 (n=4), step_factor=2, initial_step=0.5
res = derivative(f, 1.0, maxiter=3, tolerances=dict(atol=0, rtol=0))
for i, c in enumerate(calls):
    print("call", i, ":", c)

print("---")
print("df =", float(res.df), "true =", float(np.exp(1.0)))
print("status =", int(np.asarray(res.status)))
