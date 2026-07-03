本讲精读 `scipy/fftpack/_basic.py`，揭示 `fft/ifft/rfft/irfft/fftn/ifftn/fft2/ifft2` 八个公共函数的 Python 层是「薄壳」：函数体仅一行 `return _duccfft.xxx(...)`，整理参数后委托给编译后端 DUCC 计算，保证对外 API 稳定（FFTPACK 由 Fortran 迁至 DUCC 对调用者透明）。

引入术语：薄壳委托（thin shell）、`overwrite_x` 契约、精度分派、归一化约定（`norm`/`backward`）。

核心结论：一、`overwrite_x=True` 是「许可」（can be destroyed）而非「保证破坏」，属性能提示，原样透传、不影响结果正确性；二、输入精度统一分派——half→single、非浮点→double、longdouble 不支持，输出复数精度随输入走（complex64/complex128）；三、签名不暴露 `norm`，委托行硬编码的 `None` 即 `norm` 槽位，永远采用 `"backward"` 约定，故有「fft 求和、ifft 除以 n 求平均」的口诀。