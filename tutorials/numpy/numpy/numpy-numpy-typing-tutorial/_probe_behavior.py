import warnings
import numpy.typing as npt

print("NBitBase in npt.__dict__:", "NBitBase" in npt.__dict__)
print("ArrayLike in npt.__dict__:", "ArrayLike" in npt.__dict__)

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    x = npt.NBitBase
    print("warnings on npt.NBitBase:", len(w))
    for wi in w:
        print("  ", wi.category.__name__, ":", str(wi.message)[:60])

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    y = npt.ArrayLike
    print("warnings on npt.ArrayLike:", len(w))

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    from numpy.typing import NBitBase as NB
    print("warnings on from-import NBitBase:", len(w))

print("dir contains NBitBase:", "NBitBase" in dir(npt))
print("dir contains test:", "test" in dir(npt))
print("type of NBitBase:", type(npt.NBitBase).__name__)
