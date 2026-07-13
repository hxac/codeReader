import numpy as np
import numpy.ma as ma
print('default int   :', ma.default_fill_value(np.array([1,2,3], dtype='i8')))
print('default float :', ma.default_fill_value(np.array([1.,2.,3.], dtype='f8')))
print('default cplx  :', ma.default_fill_value(np.array([1+2j], dtype='c16')))
print('maxfill int8  :', ma.maximum_fill_value(np.array([1], dtype='i1')))
print('maxfill f4    :', ma.maximum_fill_value(np.array([1.], dtype='f4')))
print('minfill int8  :', ma.minimum_fill_value(np.array([1], dtype='i1')))
a = ma.array([1., 2., 3., 4.], mask=[0,0,1,0], dtype='f8')
print('a.max()       :', a.max())
print('filled view   :', a.filled(ma.maximum_fill_value(a)))
print('all-masked    :', ma.array([1.,2.], mask=[1,1]).max(axis=0))
print('struct default:', ma.default_fill_value(np.dtype('i4,f4,U5')))
