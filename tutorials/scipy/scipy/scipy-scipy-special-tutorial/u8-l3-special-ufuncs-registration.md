# _special_ufuncs.cpp / _gufuncs.cpp:新的 ufunc 注册路径

## 1. 本讲目标

本讲承接 u8-l1(xsf 内核)与 u3-l1(`functions.json` 声明表),精读两个用「纯 C++」直接注册 ufunc 的扩展模块:

- [`_special_ufuncs.cpp`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp) 与配套 [`_special_ufuncs_docs.cpp`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs_docs.cpp)
- [`_gufuncs.cpp`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp)
- 构建编排 [`meson.build`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build)

读完后你应当能够:

1. 说清 `scipy.special` 现存的**两条 ufunc 注册路径**——旧的「`functions.json` → 生成 `.pyx`」与新的「`xsf::numpy::ufunc` 在 C++ 中直接注册」,以及二者通过 `special_ufuncs` 闸门避免重复的协作方式。
2. 读懂 `xsf::numpy::ufunc({...}, "name", doc)` 这一调用:括号初始化列表里是一组「类型化函数指针别名」(如 `f_f`、`d_dddd`),`static_cast` 利用 C++ 重载在编译期为同一个数学函数挑出各 dtype 的内核。
3. 区分**普通 ufunc**(`_special_ufuncs`,逐元素、定长输出)与**广义 ufunc / gufunc**(`_gufuncs`,带「核心维度」、输出长度随运行时整数参数变化),并理解 gufunc 为何必须额外提供签名串与 `map_dims` 回调。
4. 理解**文档串外置**设计:`_special_ufuncs.cpp` 只写「内核注册」,文档字符串单独住在 `_special_ufuncs_docs.cpp`,二者编进同一个扩展模块,靠链接器把 `extern const char *` 声明与定义对接。

## 2. 前置知识

本讲假定你已读过 U3 单元(代码生成管线)与 u8-l1(xsf_wrappers)。复习几个关键概念:

- **ufunc**:NumPy 的「通用函数」,C 层对象,按 dtype 分发、逐元素求值、可批量。`scipy.special` 里几乎所有函数都是 ufunc(见 u2-l1)。一个 ufunc 内部挂多条 **loop**(类型环),如 `erf` 挂 `f->f`、`d->d`、`F->F`、`D->D` 四环。
- **类型码**:单字符描述 dtype——`f`=float32、`d`=float64、`g`=long double、`F`=cfloat、`D`=cdouble、`G`=clongdouble、`i/l`=整数(见 u3-l1 的 `CY_TYPES`/`C_TYPES` 字典)。把输入类型码拼接、下划线、再拼输出类型码,就得到一条 loop 的「签名串」,如 `dd_d` 表示 `(double,double)->double`。
- **生成式路径**:在 `functions.json` 里写一条声明,`_generate_pyx.py` 读它、生成 `_ufuncs.pyx` 里的 `PyUFunc_FromFuncAndData` 注册代码,编译进 `_ufuncs` 扩展模块(见 u3-l2、u3-l3)。
- **xsf**:SciPy 自研的现代 C++ 特殊函数库,函数住在 `xsf` 命名空间,且是**重载**的——`xsf::erf` 同时有 `float erf(float)`、`double erf(double)`、`std::complex<float> erf(...)` 等多个版本(见 u8-l1)。
- **gufunc**(generalized ufunc):NumPy 的「广义 ufunc」,允许「核心维度」——即对一个小块(矩阵、序列)而非单个标量做逐块运算,输出形状可由核心维度决定。`np.linalg.det` 就是 gufunc。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [`_special_ufuncs.cpp`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp) | **新路径主文件**。用 `xsf::numpy::ufunc(...)` 直接注册 189 个普通 ufunc(`erf`、`airy`、`jv`、`gamma` 等),外加 4 个多输出 gufunc 对象(`legendre_p` 等)。 |
| [`_special_ufuncs_docs.cpp`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs_docs.cpp) | **文档库**。定义 189 条 `const char *_xxx_doc` 字符串,与上面的 ufunc 一一对应,靠 `extern` 对接。 |
| [`_gufuncs.cpp`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp) | **gufunc 主文件**。用 `xsf::numpy::gufunc(...)` 注册输出长度随整数参数变化的广义 ufunc(`_lqn`、`_rctj`、`_poisson_binom_*` 等)。 |
| [`_generate_pyx.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py) | **闸门**。`special_ufuncs` 名单告诉生成器「这些函数已在新路径注册,生成式路径不要再生成」。 |
| [`functions.json`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json) | **旧路径声明表**。本讲用来对照:迁移到新路径的函数已从此处移除。 |
| [`meson.build`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build) | **构建编排**。把 `_*_ufuncs.cpp` + `_*_docs.cpp` + `sf_error.cc` 编进各自的扩展模块。 |

## 4. 核心概念与源码讲解

### 4.1 两条注册路径与 `special_ufuncs` 闸门

#### 4.1.1 概念说明

回顾 u3-l1/u3-l4:`scipy.special` 的 ufunc 历史上只有一条流水线——在 `functions.json` 里声明,由 `_generate_pyx.py` 生成 Cython 代码 `_ufuncs.pyx`,再编译进 `_ufuncs` 扩展模块。这条路径的好处是「声明式」:改一行 JSON 就能加一个函数。

但这条路径有个根本约束:它生成的内层循环说的是 **C 方言**,要消费 `extern "C"` 的稳定符号(这正是 u8-l1 的 `xsf_wrappers` 存在的原因)。当你想直接利用 xsf 的现代 C++ 接口(重载、模板、`std::complex`)、又不想为每个函数手写一层 C 包装时,这条路就显得笨重。

于是出现了**第二条路径**:在 `.cpp` 文件里直接调用一个 helper `xsf::numpy::ufunc(...)`,把 C++ 内核当场注册成 NumPy ufunc,完全不经过 `functions.json`、不生成 `.pyx`。这条路径产出的扩展模块就是 `_special_ufuncs`(普通 ufunc)与 `_gufuncs`(广义 ufunc)。

两条路径**并存且协作**:同一个函数不能被注册两次,否则 `import` 时会报符号冲突或 ufunc 重复。协调机制是 `_generate_pyx.py` 里的一张名单 `special_ufuncs`——凡是在这张名单里的函数,生成式路径就**跳过**它(因为它已经在新路径注册了)。

#### 4.1.2 核心流程

两条路径汇入同一个 `scipy.special._ufuncs` 命名空间,但来源不同:

```
                 ┌────────── 旧路径(生成式) ──────────┐
functions.json ──> _generate_pyx.py ──> _ufuncs.pyx ──┐
   (f not in special_ufuncs)        (生成 PyUFunc_...) │
                                                       │
                 ┌────────── 新路径(直注册) ─────────┐ │
_special_ufuncs.cpp: xsf::numpy::ufunc(...) ──> _special_ufuncs.so
_gufuncs.cpp:       xsf::numpy::gufunc(...) ──> _gufuncs.so
                                                       │
   _ufuncs.pyx 末尾: from ._special_ufuncs import (...) ┘
                        └──────────────────────────────┘
                          汇成统一的 _ufuncs 命名空间
```

关键点:`_ufuncs.pyx` 在生成时,末尾会被注入一句 `from ._special_ufuncs import (...)`(见 4.1.3),把新路径注册好的 ufunc **重新导出**进 `_ufuncs` 模块。这样从 `scipy.special` 顶层看,无论函数走哪条路径,都在同一个货架上。

#### 4.1.3 源码精读

**闸门名单**。`_generate_pyx.py` 顶部维护一个长名单,收录所有「已迁移到新路径」的函数:

- [`_generate_pyx.py:79`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L79) 开启 `special_ufuncs = [ ... ]`,一直列到第 269 行,共约 190 个名字(`"_bivariate_normal_sf"`、`"agm"`、`"airy"`、`"erf"`、`"jv"`、`"gamma"` 等)。这是 4.1.2 流程图里那道「闸门」。

**生成时过滤**。`main()` 遍历 `functions.json`,只把不在名单里的函数交给生成器:

- [`_generate_pyx.py:962-964`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L962-L964) 那句 `if (f not in special_ufuncs)` 就是闸门本体——名单里的函数被跳过,不生成 `.pyx` 注册代码。

**末尾重导出**。被跳过的函数并不会从 `_ufuncs` 命名空间消失,而是靠生成产物末尾注入的一句 import 重新进入:

- [`_generate_pyx.py:288-289`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L288-L289) `UFUNCS_EXTRA_CODE_BOTTOM` 把 `from ._special_ufuncs import ({所有 special_ufuncs 名字})` 拼到 `_ufuncs.pyx` 末尾。

**`__all__` 也并入**。生成的 `_ufuncs.__all__` 同时包含旧路径生成的名字与新路径的名字:

- [`_generate_pyx.py:898-903`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L898-L903) 先收旧路径生成的 ufunc 名,再追加 `special_ufuncs` 名单里不带下划线开头的名字,拼成最终 `__all__`。

**新路径文件的自述**。`_special_ufuncs.cpp` 顶部有一段注释,直接说明了这条路径的存在与「双登记」意图:

- [`_special_ufuncs.cpp:55-61`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L55-L61) 说明本文件用 `xsf::numpy::ufunc` 注册 ufunc、文档放在 `_special_ufuncs_docs.cpp`。注意末句「adding a ufunc, you will also need to add the appropriate entry to scipy/special/functions.json」在当前代码里已**部分过时**——见 4.1.4 实测。

#### 4.1.4 代码实践

**实践目标**:用实测确认「迁移到新路径的函数已经从 `functions.json` 移除」,亲手验证双轨现状。

**操作步骤**(纯源码阅读型,无需编译):

1. 打开 [`_generate_pyx.py:79`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L79) 的 `special_ufuncs` 名单,挑三个名字,例如 `airy`、`erf`、`agm`。
2. 在 [`functions.json`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json) 中搜索这三个名字作为顶层键(形如 `"airy": {`)。
3. 再在 [`cython_special.pxd`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd) 中搜索同样的名字。

**需要观察的现象**:

- `functions.json` 里**找不到** `"airy"`、`"erf"`、`"agm"` 这三个顶层键(它们已被移除)。
- `cython_special.pxd` 里**能找到**这三个名字的 `cpdef`/`cdef` 声明(例如 [`cython_special.pxd:29-31`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd#L29-L31) 的 `voigt_profile`、`agm`、`airy`)。

**预期结果**:这三个函数的 ufunc 内核注册在 `_special_ufuncs.cpp`、Cython 类型化声明在 `cython_special.pxd`,但**已经不在 `functions.json`**。这说明它们完成了「从生成式路径整体迁移到直注册路径」。因此 [`_special_ufuncs.cpp:60-61`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L60-L61) 那句「也要在 functions.json 登记」对这批已迁移函数不再成立——它描述的是「尚未迁移的函数」的旧约定。

#### 4.1.5 小练习与答案

**练习 1**:如果一个函数**同时**出现在 `special_ufuncs` 名单和 `functions.json` 里,会发生什么?

**参考答案**:名单里的函数会被 [`_generate_pyx.py:963`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L963) 的 `if (f not in special_ufuncs)` 跳过,不生成 `.pyx` 注册代码;它的 ufunc 只在 `_special_ufuncs.cpp` 注册一次,再由末尾的 `from ._special_ufuncs import` 引入 `_ufuncs`。所以即便 `functions.json` 残留一条声明,也不会产生重复注册——闸门已经把它挡在生成路径之外。`functions.json` 里那条残留声明此时只是「死声明」,不影响构建。

**练习 2**:`special_ufuncs` 名单为何要单独维护成一张手写列表,而不是自动从 `_special_ufuncs.cpp` 扫描得出?

**参考答案**:因为生成器 `_generate_pyx.py` 是**构建期**运行的 Python 脚本,而 `_special_ufuncs.cpp` 是**源码**。让生成器去解析 C++ 源码里的 `xsf::numpy::ufunc("name", ...)` 调用既脆弱(要写正则匹配 C++)又容易漏(多行调用、宏、`Py_BuildValue` 打包的 gufunc)。维护一张显式 Python 名单,简单、可 review、且能直接喂给 `from ._special_ufuncs import (...)` 的字符串拼接。代价是新增/迁移函数时要**两处同步**(改 `.cpp` + 改名单),这正是 4.4 节「双轨维护成本」的来源。

---

### 4.2 `xsf::numpy::ufunc`:普通 ufunc 的 C++ 直注册

#### 4.2.1 概念说明

`xsf::numpy::ufunc` 是定义在 xsf 库(`xsf/numpy.h`)里的一个 C++ 函数,它吃「一组类型化内核 + 名字 + 文档指针」,返回一个 `PyObject *`(即 NumPy ufunc 对象)。它本质上是对 NumPy C-API `PyUFunc_FromFuncAndData` 的高级封装,把旧路径里由生成器拼出来的那一大坨样板代码,浓缩成一句声明式的 C++ 调用。

它最巧妙的地方在于**用 C++ 重载 + `static_cast` 来枚举 loop(类型环)**。xsf 里的数学函数是重载的:`xsf::erf` 同时是 `float(float)`、`double(double)`、`std::complex<float>(...)` 等。注册时,你写 `static_cast<xsf::numpy::f_f>(xsf::erf)`——`xsf::numpy::f_f` 是一个「接受 float 返回 float 的函数指针类型」的别名,这个 `static_cast` 在**编译期**就把 `xsf::erf` 的 `float(float)` 重载版本挑出来、取其地址。于是同一个数学名字,被显式展开成多条类型化 loop。

这种写法把 u3-l1 里「类型码 → loop」的声明式语法,换成了等价但更地道的 C++ 表达。

#### 4.2.2 核心流程

注册一个普通 ufunc 的统一模式:

```
PyObject *变量 = xsf::numpy::ufunc(
    { <括号初始化列表:一组 static_cast<类型别名>(xsf::内核) > },
    "ufunc名字",        // 字符串字面量
    文档指针            // const char *,来自 _*_docs.cpp
);
PyModule_AddObjectRef(module, "ufunc名字", 变量);  // 挂到扩展模块
```

类型别名的命名规则与 u3-l1 的类型码一脉相承:

- 下划线**左边**是输入类型码拼接,**右边**是输出类型码拼接。
- `f_f` = `(float)->float`;`d_d` = `(double)->double`;`F_F` = `(cfloat)->cfloat`;`D_D` = `(cdouble)->cdouble`。
- `ff_f` = `(float,float)->float`;`fff_f` = 三入一出;`lf_f` = `(long,float)->float`(整数阶 + 浮点自变量,如球 Bessel)。
- **多输出**:`d_dddd` = `(double)->(double,double,double,double)`,此时调用要额外传一个输出个数 `4`。

这套编码和你在 u2-l1 里用 `.types` 看到的 `d->d`、`dddD->D` 完全对应,只是从「字符串」变成了「C++ 类型」。

#### 4.2.3 源码精读

**最简单的单输入 ufunc**——`agm`(算术-几何平均),两个实数类型环:

- [`_special_ufuncs.cpp:381-384`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L381-L384) 注册 `agm`:`{ff_f, dd_d}` 两个内核,名字 `"agm"`,文档 `agm_doc`。两个 `static_cast` 分别挑出 `xsf::agm` 的 float 与 double 重载。这段代码说明 `agm` 不支持复数(没有 `F_FF`/`D_DD` 环)。

**四类型环 ufunc**——`erf`,覆盖实/复 × 单/双精度:

- [`_special_ufuncs.cpp:577-580`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L577-L580) 注册 `erf`:`{f_f, d_d, F_F, D_D}` 四个内核。这四条 loop 正是你在 u2-l1 里用 `special.erf.types` 会看到的 `f->f`、`d->d`、`F->F`、`D->D`。

**多输出 ufunc**——`airy` 一次返回 (Ai, Aip, Bi, Bip) 四个值:

- [`_special_ufuncs.cpp:431-435`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L431-L435) 注册 `airy`:内核类型是 `f_ffff`/`d_dddd`/`F_FFFF`/`D_DDDD`(一入四出),并多了一个参数 `4`(输出个数),放在名字 `"airy"` 前面。这个 `4` 告诉 `xsf::numpy::ufunc` 构造一个 `nout=4` 的 ufunc,对应 Python 端 `airy(x)` 返回四元组的行为。

**模块初始化骨架**——所有注册都发生在 module exec 函数里:

- [`_special_ufuncs.cpp:272-282`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L272-L282) 是 `_special_ufuncs_module_exec` 的开头:先 `_import_array()` / `_import_umath()` 初始化 NumPy C-API,然后第一个注册的就是 `_bivariate_normal_sf`。文件里这样的 `xsf::numpy::ufunc(...)` 调用共有 **189 处**(可与 4.1 的名单规模相互印证)。

#### 4.2.4 代码实践

**实践目标**:从一条注册语句反推出该 ufunc 在 Python 端的类型支持,并与运行时 `.types` 互相验证。

**操作步骤**:

1. 阅读 [`_special_ufuncs.cpp:431-435`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L431-L435) 的 `airy` 注册,写下它声明的四条 loop 类型码(答案:`f->ffff`、`d->dddd`、`F->FFFF`、`D->DDDD`)。
2. 在已安装 SciPy 的环境里运行:

```python
import scipy.special as sc
print(sc.airy.types)        # 列出所有 loop
print(sc.airy(1.0))         # 标量输入,返回 4 元组
```

**需要观察的现象**:`sc.airy.types` 打印出的字符串列表,与源码里 `{f_ffff, d_dddd, F_FFFF, D_DDDD}` 一一对应(NumPy 用 `f->ffff` 这种写法表示);`sc.airy(1.0)` 返回长度为 4 的数组(对应 `nout=4`)。

**预期结果**:源码声明与运行时元数据完全吻合——证明 `xsf::numpy::ufunc` 的「括号列表」就是 `.types` 的来源。

**若无法本地运行**:明确标注「待本地验证」,仅完成源码侧推断亦可。

#### 4.2.5 小练习与答案

**练习 1**:[`_special_ufuncs.cpp:605-607`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L605-L607) 的 `wofz` 只挂了 `{F_F, D_D}` 两个环,没有 `f_f`/`d_d`。这说明 `wofz` 有什么特性?

**参考答案**:`wofz`(Faddeeva 函数 \(w(z)=e^{-z^2}\operatorname{erfc}(-iz)\))只接受**复数**输入、返回复数,不支持实数 loop。如果你传一个实数,NumPy 会先把它提升成复数再走 `D_D` 环。

**练习 2**:为什么 [`_special_ufuncs.cpp:855-859`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L855-L859) 的 `jv`(柱 Bessel J)有 `ff_f`/`dd_d`/`fF_F`/`dD_D` 四条环,其中后两条是「实数阶 + 复数自变量」?

**参考答案**:Bessel 函数 \(J_\nu(z)\) 的阶 \(\nu\) 是实数,而自变量 \(z\) 既可实也可复。`fF_F` 表示 `(float 阶, cfloat 自变量) -> cfloat`。xsf 的 `cyl_bessel_j` 为这四种组合各自提供了重载,`static_cast` 把它们分别挑出来挂成四条 loop。

---

### 4.3 `xsf::numpy::gufunc`:广义 ufunc、核心维度与自动微分

#### 4.3.1 概念说明

`_special_ufuncs.cpp` 解决的是「逐元素、定长输出」的函数。但 `scipy.special` 里有一类函数的**输出长度依赖于一个整数参数**,而不是输入数组形状。典型例子:`_lqn(n, z)` 一次性算出 0..n 阶全部 Legendre Q 多项式值,返回长度 n+1 的序列——这个 n+1 在注册时是未知的,要到运行时看参数 n 才能定。

普通 ufunc 做不到这件事,因为 NumPy 要求 ufunc 的输出形状能从输入形状经广播**静态**推出。于是需要 **gufunc**(generalized ufunc):它引入「核心维度」的概念,允许输出有一个「大小由参数决定」的维度,并要求你提供一个 `map_dims` 回调,在运行时告诉 NumPy「这个核心维度具体多大」。

`xsf::numpy::gufunc(...)` 就是注册 gufunc 的 helper,比 `ufunc` 多两个参数:一条**签名串**(描述核心维度)和一个 **`map_dims` 回调**。

此外,`_gufuncs.cpp` 与 `_special_ufuncs.cpp` 里还大量出现 `xsf::numpy::compose{xsf::numpy::autodiff(), ...}` 这种包装——它用**对偶数(dual number)** 自动微分,让一个内核同时返回「函数值 + 各阶导数」,这正是 u5-l3 里 `legendre_p_all` / `sph_harm_y_all` 一次返回多阶导数Tuple 的底层支撑。

#### 4.3.2 核心流程

注册一个 gufunc 的统一模式:

```
PyObject *变量 = xsf::numpy::gufunc(
    { <一组内核(可被 compose/autodiff 包装)> },
    输出个数,            // 整数,如 1 或 2
    "名字",
    文档指针,            // 可为 nullptr(内部函数)
    "核心维度签名串",     // 如 "()->(np1),(np1)"
    map_dims回调          // 运行时把符号维度翻译成具体大小
);
```

签名串的读法(以 `_lqn` 的 `"()->(np1),(np1)"` 为例):

- `()` 表示输入是标量(无核心维度)。
- `(np1)` 表示输出有一个核心维度,名字叫 `np1`——它的实际大小 = n+1,由 `map_dims` 在运行时填入。
- 两个 `(np1)` 表示两个输出各自带一个长度为 n+1 的轴。

`map_dims` 回调的职责:NumPy 在分配输出数组前会调用它,问「这个核心维度有多大」。回调读取输入侧的核心维度(签名里出现在输入 `()` 中的维度),按公式算出 `np1 = n+1` 之类,写回输出维度数组。

#### 4.3.3 源码精读

**`map_dims` 回调族**。文件顶部定义了一组模板回调,把符号维度翻译成具体大小:

- [`_gufuncs.cpp:34-58`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp#L34-L58) 定义 `legendre_map_dims<NOut>`、`assoc_legendre_map_dims<NOut>`、`sph_harm_map_dims`、`_poisson_binom_map_dims` 等。例如 `legendre_map_dims` 把输出维度设为 `dims[0]`(即传入的 n+1)。

**最朴素的 gufunc**——`_lqn`,一次算出 0..n 阶 Legendre Q:

- [`_gufuncs.cpp:270-274`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp#L270-L274) 注册 `_lqn`:内核 `{f_f1f1, d_d1d1, F_F1F1, D_D1D1}`(这里的 `f1` 表示「带一个核心维度」的浮点),输出个数 `2`,签名 `"()->(np1),(np1)"`(标量入,两个长 n+1 的序列出),`map_dims` 用 `legendre_map_dims<2>`。`f_f1f1` 这种类型别名说明:输入一个标量 float,输出两个「各带 1 个核心维度」的 float 数组。

**自动微分包络**——`legendre_p_all` 把 3 个 diff 阶(0/1/2)的 gufunc 打包成 tuple:

- [`_gufuncs.cpp:138-158`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp#L138-L158) 用 `Py_BuildValue("(N,N,N)", gufunc, gufunc, gufunc)` 把三个 gufunc(分别对应导数阶 0/1/2)打包成一个 tuple 对象挂到模块。每个 gufunc 的内核都用 `xsf::numpy::compose{xsf::numpy::autodiff(), ...}` 包装,使其一次返回值 + 各阶导数。这正是 u5-l3 `legendre_p_all(..., diff=n)` 的底层。

**带内部缓存的 gufunc**——`_poisson_binom_pmf`:

- [`_gufuncs.cpp:87-107`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp#L87-L107) 定义 `_poisson_binom_pmf_kernel`,内部持有一个 `std::vector<T> dist` 缓存与 `last_p_ptr` 指纹。它在 [`_gufuncs.cpp:308-319`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp#L308-L319) 被注册。文件顶部 [`_gufuncs.cpp:72-85`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp#L72-L85) 的注释解释了这种「单次调用内缓存」的迭代顺序要求(外层遍历 `p`、内层遍历 `k`,从而命中缓存)。

#### 4.3.4 代码实践

**实践目标**:对比一个普通 ufunc 与一个 gufunc 在「输出形状如何决定」上的差异。

**操作步骤**:

1. 阅读 [`_gufuncs.cpp:270-274`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp#L270-L274) 的 `_lqn`:注意它有签名串 `"()->(np1),(np1)"` 与 `map_dims` 回调——这是 gufunc 的标志。
2. 对照 [`_special_ufuncs.cpp:577-580`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L577-L580) 的 `erf`(普通 ufunc):没有签名串、没有 `map_dims`,输出形状严格等于输入形状。
3. 用 Python 验证形状行为(若环境可用):

```python
import numpy as np
import scipy.special as sc
x = np.array([0.5, 1.0])
# 普通 ufunc:输出形状 == 输入形状
print(sc.erf(x).shape)          # (2,)
# 序列型函数(经 _basic.py 包装调用 gufunc):输出长度依赖 n
print(sc.lqmn(3, 3, 0.5)[0].shape)  # 与 n、m 相关,而非 x 的形状
```

**需要观察的现象**:`erf` 对长度 2 的输入返回长度 2;而 `lqmn` 返回的形状由阶参数 `(m,n)` 决定,与 `x` 是否是数组无关。

**预期结果**:直观看到「普通 ufunc 输出形状 = 输入形状」而「gufunc 输出形状由核心维度(进而由整数参数)决定」。

**若无法本地运行**:标注「待本地验证」,源码侧的对比(有无签名串/`map_dims`)已足以说明问题。

#### 4.3.5 小练习与答案

**练习 1**:`_lqn` 的签名是 `"()->(np1),(np1)"`。请解释:为什么输入侧是 `()`(空)而不是 `(np1)`?

**参考答案**:输入 `n` 和 `z` 都是**标量**(无核心维度),所以输入侧括号里为空。`np1`(n+1)是**输出**的核心维度——它描述「输出序列有多长」,这个长度由标量参数 n 决定,由 `map_dims` 在运行时填入。如果输入本身是一个序列(如 `_poisson_binom_pmf` 的概率向量 p),那么输入侧就会出现核心维度(见其签名 `"(),(i)->()"`)。

**练习 2**:`_special_ufuncs.cpp` 里 [`L975-989`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L975-L989) 的 `legendre_p` 也用了 `xsf::numpy::gufunc` 而非 `xsf::numpy::ufunc`。为什么它「住在」`_special_ufuncs.cpp` 而不是 `_gufuncs.cpp`?

**参考答案**:模块的物理划分并不严格等同于「ufunc vs gufunc」的逻辑划分。`_special_ufuncs.cpp` 主体是普通 ufunc,但也「顺便」收了 `legendre_p`/`assoc_legendre_p`/`sph_legendre_p`/`sph_harm_y` 这几个用 gufunc 实现的新一代多输出 API;`_gufuncs.cpp` 则专门收更「经典」的序列型 gufunc(`_lqn`/`_lqmn`/`_rctj`/`_rcty`/`_poisson_binom_*`)。二者都调用同一个 `xsf::numpy::gufunc`,只是归档位置不同——这是一个历史编排,而非硬性技术约束。

---

### 4.4 文档外置:`_*_docs.cpp` 的关注点分离

#### 4.4.1 概念说明

NumPy ufunc 有一个 `__doc__` 属性,需要一段文档字符串。`scipy.special` 的文档串往往很长——例如 `hyp0f1` 的文档包含数学公式、参数表、参考文献、示例代码,动辄五六十行。如果把 189 条这样的长字符串**内联**在 `_special_ufuncs.cpp` 里,那个文件会被文档淹没:注册逻辑(才是该文件的本职)反而被挤到难以阅读。

解决方案是**关注点分离**:把所有文档字符串搬到独立的 `_special_ufuncs_docs.cpp`,在 `_special_ufuncs.cpp` 里只保留一句 `extern const char *erf_doc;` 声明。两个文件被 meson 编进**同一个扩展模块**,链接器负责把 `extern` 声明与定义对接。注册调用 `xsf::numpy::ufunc({...}, "erf", erf_doc)` 里传的 `erf_doc` 就是指向那个外部定义的指针。

`_gufuncs` 走完全相同的设计,配套文件是 `_gufuncs_docs.cpp`。

#### 4.4.2 核心流程

```
_special_ufuncs.cpp               _special_ufuncs_docs.cpp
───────────────────               ────────────────────────
extern const char *erf_doc;   ◄── 链接器对接 ──►  const char *erf_doc = R"(
                                                        erf(x, out=None)
                                                        ...五六十行...
                                                    )";
xsf::numpy::ufunc({...}, "erf", erf_doc)
                                        ▲
                  传指针(指向 docs.cpp 里的定义)
```

两个文件都是同一个扩展模块的源文件(meson.build 里列在同一个 `extension_module` 的源码列表中),所以 `extern` 在链接期天然解析到同模块内的定义,无需跨模块。

#### 4.4.3 源码精读

**注册侧的 `extern` 声明**。`_special_ufuncs.cpp` 顶部集中声明了所有文档指针:

- [`_special_ufuncs.cpp:63-66`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L63-L66) 起一连串 `extern const char *_cospi_doc;`、`extern const char *airy_doc;`、`extern const char *erf_doc;` 等(一直列到第 251 行)。这些只是声明,不带定义。

**文档侧的定义**。`_special_ufuncs_docs.cpp` 用 C++11 原始字符串字面量 `R"(...)"` 定义每条文档:

- [`_special_ufuncs_docs.cpp:1-3`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs_docs.cpp#L1-L3) 定义 `_cospi_doc`(内部函数,文档极短:`"Internal function, do not use."`)。
- [`_special_ufuncs_docs.cpp:29-93`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs_docs.cpp#L29-L93) 定义 `hyp0f1_doc`(公开函数,含 `Parameters`/`Returns`/`Notes`/`References`/`Examples` 完整段落)。该文件共定义 **189 条** `const char *`,与 `_special_ufuncs.cpp` 的 189 个 ufunc 一一对应。

**构建侧的「同模块」绑定**。meson 把两个 `.cpp` 列为同一扩展模块的源:

- [`meson.build:34-42`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L34-L42) `_special_ufuncs` 扩展模块的源码列表是 `['_special_ufuncs.cpp', '_special_ufuncs_docs.cpp', 'sf_error.cc']`。三者编进同一个 `.so`,`extern` 在模块内部解析。
- [`meson.build:44-52`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L44-L52) `_gufuncs` 扩展模块同理,源码列表含 `'_gufuncs_docs.cpp'`。

**附带:错误处理的「每模块一份」**。两个文件都各自定义了一个 `_set_action` 方法并编入 `sf_error.cc`:

- [`_special_ufuncs.cpp:255-270`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L255-L270) 定义 `_set_action` 与 `_methods` 表。这是因为 u7-l1/u7-l2 讲过:TLS 错误动作表是 `static` 的,每个扩展模块各持一份独立副本,所以 `seterr` 必须跨所有模块同步——`_special_ufuncs` 与 `_gufuncs` 也都要各自暴露 `_set_action` 入口。两模块都带 `cpp_args: ['-DSP_SPECFUN_ERROR']`([`meson.build:32`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L32))以启用 xsf 的错误挂钩。

#### 4.4.4 代码实践

**实践目标**:亲手追踪一条「文档串」从定义到挂上 ufunc 的完整路径,理解分离设计。

**操作步骤**:

1. 在 [`_special_ufuncs_docs.cpp`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs_docs.cpp) 中找到 `erf_doc` 的定义(搜索 `const char *erf_doc`),阅读其内容(应是一段 NumPy 风格 docstring)。
2. 在 [`_special_ufuncs.cpp`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp) 中确认:顶部有 `extern const char *erf_doc;`(声明),`erf` 的注册语句 [`L577-580`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L577-L580) 把 `erf_doc` 作为第三个参数传入。
3. 在 [`meson.build:34-42`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L34-L42) 确认两个 `.cpp` 在同一个 `extension_module` 的源码列表里。
4. (若环境可用)运行 `python -c "import scipy.special as sc; print(sc.erf.__doc__[:60])"`,对比打印内容是否就是 `erf_doc` 字符串的开头。

**需要观察的现象**:文档串在 docs 文件里**定义**、在注册文件里**声明并传指针**、在 meson 里**同模块编译**、在 Python 端通过 `__doc__` **可见**——四个环节闭环。

**预期结果**:四个环节的文本一致,证明 `extern` + 同模块链接 + 指针传参的设计完整生效。

#### 4.4.5 小练习与答案

**练习 1**:为什么不把文档串直接写在 `_special_ufuncs.cpp` 的注册语句里(像 `"erf"` 名字那样内联)?

**参考答案**:两个原因。其一,`scipy.special` 的文档串极长(含公式、示例),189 条内联会让注册文件膨胀到难以阅读,注册逻辑被淹没。其二,文档串经常由文档团队单独修订(修错别字、补示例),把它隔离在 `_special_ufuncs_docs.cpp` 里,文档变更的 diff 不会干扰注册逻辑的 review。`"erf"` 这种名字是短字面量、几乎不变,所以内联无妨。

**练习 2**:`_special_ufuncs.cpp` 里有些 ufunc 的文档指针以 `_` 开头(如 `_igam_fac_doc`、`_kolmogc_doc`),它们在 [`_special_ufuncs_docs.cpp:9-23`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs_docs.cpp#L9-L23) 里的内容是什么?为什么?

**参考答案**:这些是**内部函数**(下划线前缀,不暴露到 `scipy.special` 顶层),其文档串统一是 `"Internal function, do not use."`。它们被注册成 ufunc 是为了供其他 `scipy.stats`/`scipy.special` 函数在 C 层高效复用,而非给最终用户调用,所以不写面向用户的文档。

---

## 5. 综合实践

把本讲三条主线——**直注册语法**、**文档外置**、**双轨现状**——串成一个完整的追踪任务。

**任务**:以 `voigt_profile` 为对象,完成「一条函数从 C++ 内核到 Python 文档」的全链路考证,并判断它走的是哪条注册路径。

**步骤**:

1. **定位注册语句**。在 [`_special_ufuncs.cpp`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp) 中搜索 `voigt_profile`,找到它的 `xsf::numpy::ufunc({...}, "voigt_profile", voigt_profile_doc)` 注册语句(约在 [`L600-603`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L600-L603))。记下它的内核类型别名(应为 `{fff_f, ddd_d}`,即三入一出、仅实数)。

2. **追踪文档来源**。在同一文件顶部确认 `extern const char *voigt_profile_doc;` 声明;再到 [`_special_ufuncs_docs.cpp`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs_docs.cpp) 找到 `voigt_profile_doc` 的定义,确认它是完整 docstring 而非 `"Internal function..."`。

3. **确认双轨状态**。在 [`functions.json`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json) 中搜索 `"voigt_profile"` 作为顶层键——预期**找不到**(已迁移);在 [`_generate_pyx.py`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py) 的 `special_ufuncs` 名单里确认 `"voigt_profile"` **在列**(约在 [`L258`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L258))。这两点共同证明它走的是**新路径(直注册)**,旧路径已被闸门关闭。

4. **核验类型化 API**。在 [`cython_special.pxd`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd) 中找到 `voigt_profile` 的 `cpdef` 声明(见 [`L29`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd#L29)),签名应是 `cpdef double voigt_profile(double, double, double) noexcept nogil`——与 ufunc 的 `ddd_d` 环同源。

5. **(可选)运行时核验**。若环境可用:

```python
import scipy.special as sc
print(sc.voigt_profile.types)          # 期望含 'fff->f','ddd->d'
print(type(sc.voigt_profile))          # <class 'numpy.ufunc'>
print(sc.voigt_profile(0, 1, 1))       # Voigt profile 在峰心、sigma=1,gamma=1 的值
```

**预期产出**:一张表,记录 `voigt_profile` 在 5 个位置(注册语句、extern 声明、docs 定义、functions.json 是否在列、special_ufuncs 是否在列、cython_special.pxd 声明)的「取证结果」,并据此给出结论:「该函数走直注册路径,文档外置,已从生成式路径完全迁移」。

## 6. 本讲小结

- `scipy.special` 现有**两条 ufunc 注册路径**:旧的「`functions.json` → `_generate_pyx.py` → `_ufuncs.pyx`」(生成式)与新的「`xsf::numpy::ufunc` / `gufunc` 在 `.cpp` 中直接注册」(直注册,产出 `_special_ufuncs` 与 `_gufuncs` 两个扩展模块)。
- 协作靠 `special_ufuncs` 闸门:`_generate_pyx.py` 用 `if (f not in special_ufuncs)` 跳过已迁移函数,再在生成产物末尾用 `from ._special_ufuncs import (...)` 把它们重新并入 `_ufuncs` 命名空间与 `__all__`。
- 直注册的核心语法是 `xsf::numpy::ufunc({static_cast<类型别名>(xsf::内核), ...}, "name", doc)`:括号初始化列表枚举各 dtype 的 loop,`static_cast` 在编译期挑出 C++ 重载——类型别名(`f_f`/`d_dddd`/`F_FFFF`...)与 u3-l1 的类型码同源。
- 普通 ufunc 与 gufunc 的区别在「输出形状是否依赖整数参数」:gufunc 额外需要签名串(如 `"()->(np1),(np1)"`)与 `map_dims` 回调,把符号核心维度翻译成运行时大小;`autodiff` 包装还让一个内核同时返回值与各阶导数。
- 文档串外置到 `_special_ufuncs_docs.cpp` / `_gufuncs_docs.cpp`,靠 `extern const char *` + 同模块链接对接,让注册文件只关注注册逻辑;两个模块各自带 `_set_action` 与 `-DSP_SPECFUN_ERROR`,承接 u7 的每模块一份 TLS 错误动作表。
- 迁移趋势:`erf`/`airy`/`agm`/`voigt_profile` 等已**整体迁出** `functions.json`(只留 `special_ufuncs` 名单 + `cython_special.pxd` 声明),新函数默认走直注册路径;`_special_ufuncs.cpp:60-61` 那句「也要在 functions.json 登记」对这批已迁移函数已部分过时。

## 7. 下一步学习建议

- **回到生成式路径的细节**:若想对照「旧路径如何用 `PyUFunc_FromFuncAndData` 注册」,复习 u3-l2(`_generate_pyx.py` 的 `Ufunc.generate`)与 u3-l3(`meson.build` 如何把生成的 `.pyx` 编进 `_ufuncs`)。
- **深入 xsf 内核**:本讲的 `xsf::erf`、`xsf::airy` 等内核的真正实现在 xsf 库;u8-l1 讲了 `xsf_wrappers` 如何把 xsf 桥接给旧路径,u8-l2 讲了 Boost 内核——可顺带理解「为何有些函数仍留在生成式路径(需混编 Boost)而非迁到直注册」。
- **错误处理闭环**:本讲提到的 `_set_action` 与 `-DSP_SPECFUN_ERROR`,其完整机制在 u7-l1(C 层 `sf_error`/TLS/GIL 桥)与 u7-l2(`seterr`/`geterr`/`errstate` 的字符串↔枚举映射),建议读完以理解「为何每个扩展模块都要一份 `_set_action`」。
- **多输出 API 的上层**:本讲 `legendre_p_all`/`sph_harm_y_all` 的 `autodiff` gufunc,在 Python 层被 u5-l3 的 `MultiUFunc` 类聚合成单一可调用对象;读 u5-l3 可看懂「C++ 直注册的多输出 gufunc」如何被 Python 中间层消费。
