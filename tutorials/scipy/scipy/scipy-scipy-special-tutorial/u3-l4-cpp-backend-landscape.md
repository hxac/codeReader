# C/C++ 后端版图:xsf、Boost、Cephes、cdflib、specfun

## 1. 本讲目标

本讲是「代码生成管线」单元(U3)的第四讲,也是收官讲。前三讲我们沿着**同一条主链**往下走:

- u3-l1 讲清了 `functions.json` 的**声明语法**(函数名 → 头文件 → 内核 → 类型签名);
- u3-l2 讲清了 `_generate_pyx.py` 如何把声明**翻译**成 `_ufuncs.pyx`;
- u3-l3 讲清了 `meson.build` 如何把 `.pyx` **编译链接**成可加载的 `.so`。

但在这一切之下,始终有一个我们**故意绕开**的核心问题:**那些「内核函数」到底从哪里来?** 当 `functions.json` 写下 `"xsf_wrappers.h"` 或 `"boost_special_functions.h++"` 时,它指向的是谁?这些 C/C++ 函数又是谁写的、什么时候写的、为什么会有这么多套?

本讲就来回答这个问题。`scipy.special` 的数值内核**不是一个统一的库**,而是由**五套来源、年代、风格各异的 C/C++ 数学库**拼装而成:现代化的自研库 **xsf**、第三方的 **Boost.Math**、经典的 **Cephes**、概率分布专用的 **cdflib**、以及 Zhang & Jin 的 **specfun**。学完本讲,你应当能够:

1. 识别这五套后端各自的**历史定位**与职责边界,知道哪类函数归谁管。
2. 读懂 `functions.json` 的「头文件」字段如何**调度**到具体后端实现,并能判断某条声明用的是 C 还是 C++、新库还是遗留库。
3. 理解一个正在发生的**迁移趋势**:几乎所有内核都在向统一的 **xsf** C++ 库收敛,而注册方式也在从「JSON 生成」向「纯 C++ 直注册」迁移。

一句话定位:前三讲讲的是**怎么把声明变成可调用的 ufunc**,本讲讲的是**声明背后真正干活的那些 C/C++ 内核是谁**。

## 2. 前置知识

阅读本讲前,最好了解以下概念(不熟悉也不要紧,下面会顺带解释):

- **特殊函数(special function)**:在物理、统计、工程中反复出现的「有名有姓」的数学函数,如 Bessel 函数、Gamma 函数、误差函数 erf、椭圆积分、超几何函数等。它们大多没有初等闭式解,需要数值算法(级数、连分式、递推、渐近展开)来计算。
- **C 与 C++ 的互操作(`extern "C"`)**:C++ 会「改名(name mangling)」函数符号,而 C 不会。要让 C++ 编译的函数能被 C(或 Cython 的 C 后端)调用,需要用 `extern "C"` 关闭改名。本讲会看到 `xsf_wrappers.h` 正是这么做的。
- **复数的两种表示**:NumPy 用自己的 `npy_cdouble`(一个 `{double real, imag;}` 结构),C++ 标准库用 `std::complex<double>`。两者内存布局兼容但不能直接互传,需要桥接函数。
- **Boost.Math policy**:Boost.Math 库提供的一种「策略」机制,可以全局关闭类型提升(`promote_float`/`promote_double`)、并把错误处理函数替换成用户自定义的实现。本讲会看到 SciPy 用它把 Boost 的 C++ 异常翻译成 Python 的告警/异常。

如果你读过 u3-l1(`functions.json` 语法)、u3-l2(代码生成器)和 u3-l3(编译目标),本讲会非常自然;否则建议先扫一眼那三讲的结论。本讲与 u3-l3 的分工是:u3-l3 关心**「这些源码被编译进了哪个扩展模块」**,本讲关心**「这些源码本身是什么库、谁写的、为什么选它」**。

## 3. 本讲源码地图

本讲聚焦的文件:

| 文件 | 角色 |
| --- | --- |
| [functions.json](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json) | **后端调度总开关**。每条 ufunc 声明的「头文件」字段决定它用哪个后端、用 C 还是 C++。本讲反复回到这里做统计与判别。 |
| [xsf_wrappers.h](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/xsf_wrappers.h) / [xsf_wrappers.cpp](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/xsf_wrappers.cpp) | **xsf 后端的 C 包装层**。把 xsf C++ 库(以及折叠进 xsf 的 Cephes)的函数包成 `extern "C"`、`npy_cdouble` 友好的接口,供 Cython/ufunc 层调用。 |
| [boost_special_functions.h](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h) | **Boost.Math 后端的接入层**。定义 `SpecialPolicy` 策略、自定义错误处理函数,并把 Boost 的 `ibeta`/`hypergeometric_1F1` 等包成 `*_float`/`*_double` 双内核。 |
| [_legacy.pxd](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_legacy.pxd) | **Cephes 遗留路径**。为那些「历史上静默把 double 截断成 int」的函数提供带告警的 `_unsafe` 包装,底层转发到 `cephes_*_wrap`。 |
| [cdflib.h](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cdflib.h) | **cdflib 后端**。一套从 Fortran 重写为 C 的概率分布 CDF/分位数库(F、非中心 F、t 等),编译为静态库 `cdflib_lib` 供 `_ufuncs` 链接。 |
| [_generate_pyx.py](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py) | 提供 `special_ufuncs` 名单(约 189 个)与生成闸门,是理解「两条注册路径」分工的关键。 |
| [meson.build](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build) | 把上述各后端**按语言/来源拆分**到不同扩展模块,并各自挂上正确依赖(`xsf_dep`、`boost_math_dep`、`cdflib_lib`、`ellint_dep`)。 |

> 提示:`xsf` 库本身的头文件(如 `<xsf/airy.h>`、`<xsf/cephes/igam.h>`)在本检出里**看不到**——它以 Meson 子项目(subproject)形式在构建期拉取,`xsf_dep` 来自[根 meson.build](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/meson.build) 的 `xsf = subproject('xsf')`(参见 u3-l3)。本讲能直接读到的,是它在本目录的**包装层** `xsf_wrappers.*`。

---

## 4. 核心概念与源码讲解

### 4.1 后端调度:`functions.json` 的头文件字段与两条注册路径

#### 4.1.1 概念说明

「后端(backend)」在这里指的是:**一个 ufunc 真正用来算数的 C/C++ 函数体从哪个库来**。同样是算 `betainc`,底层既可以用 Boost.Math 的 `ibeta`,也可以(历史上)用 Cephes 的 `incbet`。选择哪一个,就是「后端调度」。

在 `scipy.special` 里,这个调度**不是用 if/else 写在代码里**,而是**声明式**地写在 `functions.json` 的**头文件字段**里。回顾 u3-l1 讲过的三层嵌套:

```
函数名(ufunc 的 Python 名)
  └── 头文件(决定后端 + 语言)
        └── 内核函数名: 类型签名
```

中间这一层「头文件」就是后端开关。它的取值有明确约定:

| 头文件字段 | 后端 | 语言 | 判别特征 |
| --- | --- | --- | --- |
| `xsf_wrappers.h` | **xsf**(含折叠进来的 Cephes) | C 接口(`extern "C"`) | 文件名 `.h` |
| `boost_special_functions.h++` | **Boost.Math** | C++ | 文件名以 `++` 结尾 |
| `_legacy.pxd` | **Cephes 遗留**(带 `_unsafe` 截断告警) | Cython 头 | `.pxd` |
| `_cdflib_wrappers.pxd` | **cdflib** | Cython 头 | `.pxd` |
| `_ellip_harm.pxd` / `_cosine.h` / `sf_error.pxd` | 专项小内核 | Cython/C | 各自专项 |

> **关键判别法则(来自 u3-l1)**:看头文件名是否以 `++` 结尾 —— 是则是 C++ 内核(进 `_ufuncs_cxx`),否则是 C 内核(进 `_ufuncs`)。`.pxd` 表示该内核由 Cython 头声明、实现在别处。

#### 4.1.2 核心流程:两条注册路径

但「头文件字段」只描述了**一条**路径。事实上 `scipy.special` 现在有**两条**把 C/C++ 内核注册成 ufunc 的路径,这正是理解后端版图的全局骨架:

1. **JSON 生成路径**(u3-l1~u3-l3 讲的那条):`functions.json` → `_generate_pyx.py` → `_ufuncs.pyx`(C 内核)/ `_ufuncs_cxx.pyx`(Boost 等 C++ 内核)→ 编译。当前 `functions.json` 里有约 **128 条**这样的声明。
2. **C++ 直注册路径**:在 `_special_ufuncs.cpp` / `_gufuncs.cpp` 里,用 xsf 提供的 `xsf::numpy::ufunc` 模板**直接**在 C++ 里注册 ufunc,完全不经过 `functions.json` 与代码生成。这条路径上的函数名列在 `_generate_pyx.py` 的 `special_ufuncs` 名单里,共 **189 个**(如 `airy`、`erf`、`jv`、`gamma`)。

两条路径的「分工闸门」就在 `_generate_pyx.py` 的主循环里:遍历 `functions.json` 时,**凡是名字出现在 `special_ufuncs` 名单里的,就跳过、不生成**——因为它们已经由 C++ 直注册路径接管了。

```python
for f, sig in functions.items():
    if (f not in special_ufuncs):      # 闸门:已被 C++ 直注册接管的,跳过
        ufuncs.append(Ufunc(f, sig))
```

这就解释了一个容易让初学者困惑的现象:你会在 `functions.json` 里**找不到** `erf`、`airy`、`gammainc` 这些「明星函数」——它们的声明已被移出 JSON,改由 `_special_ufuncs.cpp` 全权注册。这正是「迁移趋势」的直接证据。

#### 4.1.3 源码精读

先看 JSON 生成路径里三种典型后端声明并排出现的样子。`bdtr`(二项分布 CDF)同时挂了**两个**后端——遗留的 `_legacy.pxd` 和现代的 `xsf_wrappers.h`,由类型分发(见 u3-l1 的 `iter_variants`)决定实际用哪个:

[functions.json:25-32](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L25-L32) —— `bdtr` 声明:`bdtr_unsafe`(遗留,`ddd->d`)与 `cephes_bdtr_wrap`(xsf/Cephes,`dpd->d`,注意中间参数是 `p`=指针宽度整数)两个内核并存。

再看纯 Boost 后端的 `betainc`(不完全 Beta 函数),它只有 Boost 一个来源,且提供了 float/double 双内核:

[functions.json:67-72](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L67-L72) —— `betainc` 声明:头文件 `boost_special_functions.h++`(末尾 `++` ⇒ C++),内核 `ibeta_float`/`ibeta_double`。

最有趣的混合案例是 `hyp1f1`(合流超几何函数):**实数走 Boost,复数走 xsf**。同一个 ufunc、两种后端,按输入是实数(`d`)还是复数(`D`)分发:

[functions.json:296-303](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L296-L303) —— `hyp1f1`:`hyp1f1_double`(`ddd->d`,Boost)负责实数,`chyp1f1_wrap`(`ddD->D`,xsf)负责复数。

最后看 cdflib 后端,负责非中心 F 分布的「按自由度求逆」这类操作:

[functions.json:402-406](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L402-L406) —— `ncfdtridfd`:头文件 `_cdflib_wrappers.pxd`,内核同名 `ncfdtridfd`(`dddd->d`)。

而两条路径的闸门,在生成器主循环里:

[_generate_pyx.py:960-964](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L960-L964) —— 读 JSON 后逐条判断:`f not in special_ufuncs` 才进入 `Ufunc` 构造与生成。

#### 4.1.4 代码实践

**实践目标**:亲手在 `functions.json` 里做一次后端普查,验证「头文件字段 = 后端开关」。

**操作步骤**:

1. 进入 `scipy/special` 目录。
2. 用下面的命令分别统计四种头文件字段出现的次数(每出现一次 = 有一个函数声明指向该后端):

```bash
cd scipy/special
echo "xsf:    $(grep -c '"xsf_wrappers.h":'  functions.json)"
echo "boost:  $(grep -c '"boost_special_functions.h++":' functions.json)"
echo "legacy: $(grep -c '"_legacy.pxd":'     functions.json)"
echo "cdflib: $(grep -c '"_cdflib_wrappers.pxd":' functions.json)"
```

3. 再确认「明星函数已迁出 JSON」:

```bash
grep -E '"(erf|airy|gammainc|jv)":' functions.json || echo "已迁出(走 _special_ufuncs.cpp 直注册)"
```

**需要观察的现象**:xsf 的计数应远高于其它三者(约 80+),boost 与 legacy 各十几条,cdflib 仅个位数;`erf`/`airy` 等在 JSON 里查无此名。

**预期结果**:大致为 xsf ≈ 84、boost ≈ 18、legacy ≈ 16、cdflib = 4。xsf 一家独大,印证「内核向 xsf 收敛」的趋势;`erf` 等已不在 JSON。

> 待本地验证:精确数字会随版本微调,以上是基于当前 HEAD `8e93e0478c` 的统计。

#### 4.1.5 小练习与答案

**练习 1**:不看上面的表,只看 `functions.json` 里某条声明的头文件名,如何一秒判断它会被编译进 `_ufuncs`(C)还是 `_ufuncs_cxx`(C++)?

**答案**:看头文件名是否以 `++` 结尾。`boost_special_functions.h++` 结尾有 `++` ⇒ C++ ⇒ 进 `_ufuncs_cxx`;`xsf_wrappers.h`、`_legacy.pxd` 等无 `++` ⇒ C ⇒ 进 `_ufuncs`。这是 u3-l1 已确立、本讲反复使用的判别法则。

**练习 2**:`hyp1f1` 这个 ufunc 留在了 JSON 里(未被 C++ 直注册路径接管),却又声明了 Boost 与 xsf 两个后端。请解释:为什么它没有走 `special_ufuncs` 那条纯 C++ 直注册路径?

**答案**:因为它需要**复数版本**(`chyp1f1_wrap`,`ddD->D`),而当前的纯 C++ 直注册路径(`_special_ufuncs.cpp`)尚未为它提供完整的复数实现,于是它仍留在 JSON 生成路径里,用「Boost 出实数 + xsf 出复数」的多后端分发来凑齐所有类型环。

---

### 4.2 xsf:现代化的统一 C++ 特殊函数库

#### 4.2.1 概念说明

**xsf** 是 **extended special functions** 的缩写,是 SciPy 社区**自研的现代 C++ 特殊函数库**,以独立 Git 仓库维护、作为 Meson 子项目引入。它的定位有三层:

1. **统一现代化重写**:把历史上散落在 Cephes(C)、specfun(Fortran)、各处手写代码里的算法,用现代 C++(`std::complex`、模板、命名空间)统一重写,放在 `xsf::` 命名空间下。
2. **吸收遗留库**:连 Cephes 和 specfun 也被「折叠」进来——你会在 `xsf_wrappers.cpp` 里看到 `<xsf/cephes/igam.h>`、`<xsf/specfun/specfun.h>` 这样的包含路径,说明它们已成为 xsf 树下的子目录。
3. **同时服务两条注册路径**:既被 `xsf_wrappers.*` 包装后喂给 JSON 生成路径,又被 `_special_ufuncs.cpp` 直接 `include` 用于 C++ 直注册路径。

换言之,xsf 是整个 special 后端版图的**收敛终点**。

#### 4.2.2 核心流程

xsf 后端从「C++ 算法」到「ufunc 可调用内核」要经过 `xsf_wrappers.*` 这一包装层,它解决三个问题:

1. **命名 / 链接**:`xsf::airy` 是 C++ 符号(带 mangling),ufunc 的 C 循环要的是 `extern "C"` 符号。包装层把每个 xsf 函数包成 `extern "C"` 的、扁平命名的(如 `special_airy`、`xsf_erf`)C 可调用函数。
2. **复数类型桥接**:xsf 内部用 `std::complex<double>`,而 NumPy/Cython 层用 `npy_cdouble`。两者内存兼容但类型不同,包装层用 `to_complex` / `to_ccomplex` 在边界上来回转换。
3. **多返回值**:像 Airy 函数一次要返回 4 个值(Ai, Ai', Bi, Bi'),C 不能返回多个值,于是用**指针输出参数**(`void special_airy(double x, double *ai, double *aip, double *bi, double *bip)`),这与 ufunc 的多输出机制(见 u2-l1 的 `out=`)对接。

包装层的产物(一堆 `extern "C"` 函数)声明在 `xsf_wrappers.h`,实现在 `xsf_wrappers.cpp`,前者被 `functions.json` 的头文件字段引用、后者被 `cython_special` 等扩展模块编译链接(见 u3-l3)。

#### 4.2.3 源码精读

先看 `xsf_wrappers.h` 顶部的**来源说明**与 `extern "C"` 包裹——这段注释坦白交代了它的血统:源自 Zhang & Jin 的 Fortran specfun 库,由 Travis Oliphant 做接口,「与 cephes 一起编译」:

[xsf_wrappers.h:1-9](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/xsf_wrappers.h#L1-L9) —— 包装层的身世注释:本是 Shanjie Zhang & Jianming Jin 的 Fortran 特殊函数库,经 Oliphant 接口化,与 cephes 一同编译。

[xsf_wrappers.h:18-20](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/xsf_wrappers.h#L18-L20) —— `#ifdef __cplusplus extern "C"`:无论被 C 还是 C++ 编译单元包含,都导出**未改名**的 C 符号,确保 Cython/ufunc 层能按名链接。

再看实现侧的两件核心事。其一是**复数桥接**:

[xsf_wrappers.cpp:67-69](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/xsf_wrappers.cpp#L67-L69) —— `to_complex` / `to_ccomplex`:在 `npy_cdouble` 与 `std::complex<double>` 之间无拷贝地往返转换。

其二是**复数超几何函数的转发**——它把 `npy_cdouble` 输入转成 `std::complex`,调用 `xsf::hyp1f1`,再把结果转回 `npy_cdouble`,正好对应 4.1.3 里 `hyp1f1` 的复数类型环 `ddD->D`:

[xsf_wrappers.cpp:73](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/xsf_wrappers.cpp#L73) —— `chyp1f1_wrap`:`to_ccomplex(xsf::hyp1f1(a, b, to_complex(z)))`,一行完成「转入 → 调 xsf → 转出」。

而对比一个**纯实数**的包装就简单得多,无需复数桥接,直接转发:

[xsf_wrappers.cpp:97](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/xsf_wrappers.cpp#L97) —— `special_entr(double x)` 直接 `return xsf::entr(x);`,因为 entr(信息熵)只处理实数。

最后,`xsf_wrappers.cpp` 的 include 区一览无余地展示了 xsf 的「大一统」胃口——既有 xsf 自研模块,也吞下了整个 cephes 子树:

[xsf_wrappers.cpp:4-44](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/xsf_wrappers.cpp#L4-L44) —— 包含几十个 `<xsf/*.h>` 自研模块(airy、bessel、gamma、erf、struve……)。

[xsf_wrappers.cpp:46-61](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/xsf_wrappers.cpp#L46-L61) —— 紧接着包含 `<xsf/cephes/*.h>`:Cephes 已成为 xsf 下的子目录,这就是「Cephes 折叠进 xsf」的物证。

#### 4.2.4 代码实践

**实践目标**:体会「实数内核 vs 复数内核」在包装层的差异,并验证一个 xsf 后端 ufunc 的复数能力。

**操作步骤**:

1. 在 `xsf_wrappers.cpp` 中定位 `chyp1f1_wrap`(第 73 行)与 `hyp1f1_wrap`(第 79 行),对比前者(复数)用了 `to_complex/to_ccomplex`、后者(实数)没有。
2. 在 Python 中验证 `hyp1f1` 的复数类型环确实来自 xsf:

```python
import scipy.special as sc
print(sc.hyp1f1.types)            # 应能看到实数与复数两类环
print(sc.hyp1f1(1, 2, 1+1j))      # 复数输入,走 xsf 的 chyp1f1_wrap
```

**需要观察的现象**:`.types` 里同时列出实数(`ddd->d`)与复数(`DDD->D` 等)两类环;复数输入能正常返回复数结果而不报错。

**预期结果**:`hyp1f1(1, 2, 1+1j)` 返回一个形如 `(a+bj)` 的复数,证明 xsf 复数后端在工作。

> 待本地验证:不同 SciPy 版本下 `.types` 字符串集合可能略有差异,但应同时含实数与复数环。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `xsf_wrappers.h` 要用 `extern "C"` 包裹,而 `boost_special_functions.h` 不用?

**答案**:因为两者服务的注册路径不同。`xsf_wrappers.h` 的函数要被 **C 编译单元**(ufunc 的 C 内层循环、Cython 的 C 后端)按名链接,C 不认识 C++ 的 name mangling,所以必须 `extern "C"`。而 `boost_special_functions.h++` 走的是 **C++ 路径**(`_ufuncs_cxx.pyx` → C++),整个编译链都是 C++,无需去名。

**练习 2**:`xsf_wrappers.cpp` 里同时 `#include <xsf/airy.h>` 和 `#include <xsf/cephes/igam.h>`。这说明 Cephes 与 xsf 是什么关系?

**答案**:Cephes 已被**折叠**进 xsf,作为 `xsf/cephes/` 子目录存在。xsf 既是自研新实现,也是遗留 Cephes 算法的宿主——这就是「xsf 作为收敛终点」的含义。

---

### 4.3 Boost.Math:策略化的重型 C++ 后端

#### 4.3.1 概念说明

**Boost.Math** 是著名的 Boost C++ 库家族中的数学子库,以**精度高、覆盖广、跨平台一致**著称,常被当作「黄金参考」。SciPy 把它引入 special,专门负责那些**对精度和数值稳定性要求极高、且 Cephes/xsf 一时难以覆盖好**的函数族——典型是不完全 Beta 函数(`betainc` 系列)、合流超几何函数实数版(`hyp1f1`)、各种概率分布的分位数(`*_ppf`)。

但 Boost.Math 开箱即用并不完全合 SciPy 的意,有两点必须改造:

1. **类型提升**:Boost 默认会把 `float` 提升成 `double` 再算。但 SciPy 的 ufunc 要保留 float32 类型环(见 u2-l1),所以必须**关闭提升**。
2. **错误处理**:Boost 默认抛 C++ 异常。SciPy 要的是统一的 `sf_error` 机制(返回 NaN/inf + 可配置告警,见 u2-l3),所以必须把 Boost 的错误**桥接**成 Python 告警/异常。

这两点改造都通过 Boost 的 **policy(策略)** 机制集中完成,这就是 `boost_special_functions.h` 的核心。

#### 4.3.2 核心流程

Boost 后端的接入流程:

1. 定义一个 `SpecialPolicy`(策略类型),在其中关闭 `promote_float`/`promote_double`、设置 `max_root_iterations`、并指定 `user_evaluation_error`/`user_overflow_error` 等自定义错误处理。
2. 自定义错误处理函数体内,**获取 GIL**(`PyGILState_Ensure`)后调用 `PyErr_WarnEx`(发 RuntimeWarning)或 `PyErr_SetString`(抛 OverflowError),把 Boost 的 C++ 错误翻译成 Python 信号。
3. 为每个函数写一个 `*_wrap` 模板,先做 NaN/定义域预检查并调 `sf_error`,再用 `try/catch` 包裹 `boost::math::xxx(..., SpecialPolicy())`,把 `domain_error`/`overflow_error`/`underflow_error` 等异常映射成对应的 `SF_ERROR_*`。
4. 提供 float/double **双内核**(如 `ibeta_float`/`ibeta_double`),让生成器按类型分发(见 u3-l1),保证 float32 输入得到 float32 输出。

#### 4.3.3 源码精读

先看策略定义与 Boost 头文件包含:

[boost_special_functions.h:8-16](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L8-L16) —— 包含 Boost.Math 的 beta/erf/gamma/hypergeometric 等头文件。

[boost_special_functions.h:18-22](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L18-L22) —— `SpecialPolicy`:关闭 `promote_float`/`promote_double`(保住 float32)、限制 `max_root_iterations<400>`、`discrete_quantile` 取 `real`。这是所有 Boost 调用的统一策略。

再看两个**自定义错误处理**函数——它们是把 Boost 异常翻译成 Python 信号的桥梁:

[boost_special_functions.h:36-50](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L36-L50) —— `user_evaluation_error`:拼好错误消息后,`PyGILState_Ensure()` + `PyErr_WarnEx(PyExc_RuntimeWarning, ...)` 发告警,然后**返回原值**(best guess),不中断计算。

[boost_special_functions.h:53-69](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L53-L69) —— `user_overflow_error`:同样获取 GIL,但调 `PyErr_SetString(PyExc_OverflowError, ...)` 抛异常。注意这里隔着 GIL——Boost 内核可能跑在 nogil 区,要发 Python 信号必须先拿回 GIL(与 u7 将讲的 `sf_error_v` 同理)。

接着看一个完整的 wrap 函数 `ibeta_wrap`——它体现了「预检查 + try/catch + sf_error」的标准模式:

[boost_special_functions.h:71-133](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L71-L133) —— `ibeta_wrap`:先 NaN/定义域检查并 `sf_error("betainc", SF_ERROR_DOMAIN, ...)`,再处理 `(a,b)→(0,0)` 等极限情形(返回 NaN),最后 `try { y = boost::math::ibeta(a, b, x, SpecialPolicy()); }` 并把 domain/overflow/underflow/其它异常分别映射成 `SF_ERROR_*`。

最后是 float/double **双内核**的入口,正是 `functions.json` 里 `ibeta_float`/`ibeta_double` 指向的目标:

[boost_special_functions.h:135-145](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L135-L145) —— `ibeta_float`/`ibeta_double` 只是模板 `ibeta_wrap<Real>` 的两个具体实例化,分别对应 `fff->f` 与 `ddd->d` 两个类型环。

#### 4.3.4 代码实践

**实践目标**:验证 Boost 后端的 float32 类型环确实被保留(即 `promote_float<false>` 生效),并触发一次它的定义域错误处理。

**操作步骤**:

```python
import scipy.special as sc
import numpy as np

# 1. 验证 betainc 保留了 float32 类型环(Boost 双内核在工作)
x = np.float32(0.3)
r = sc.betainc(np.float32(2), np.float32(3), x)
print("betainc types:", sc.betainc.types)   # 应含 'fff->f' 与 'ddd->d'
print("result dtype:", r.dtype)             # 期望 float32,而非被提升成 float64

# 2. 触发定义域错误(走 ibeta_wrap 里的 sf_error("betainc", SF_ERROR_DOMAIN))
print("betainc(-1, 2, 0.5):", sc.betainc(-1, 2, 0.5))   # a<0 ⇒ NaN
```

**需要观察的现象**:第 1 步结果 dtype 为 `float32`;`.types` 同时列出 `fff->f` 与 `ddd->d`。第 2 步返回 `nan`。

**预期结果**:`r.dtype == dtype('float32')`;`betainc(-1, 2, 0.5)` 返回 `nan` 且默认不抛异常(因为 domain 默认 ignore,见 u2-l3)。

> 待本地验证:不同 SciPy 版本下 `.types` 字符串集合可能略有差异,但 `fff->f` 与 `ddd->d` 应都在。

#### 4.3.5 小练习与答案

**练习 1**:`SpecialPolicy` 里为什么必须设 `promote_float<false>`?不设会怎样?

**答案**:Boost.Math 默认会把 `float` 参数提升成 `double` 计算,返回 `double`。若不关闭,则 `ibeta_float(float, float, float)` 实际返回 `double`,无法喂给 ufunc 的 `fff->f`(float32)类型环——结果要么编译期类型不符,要么运行时 float32 输入被悄悄提升成 float64,破坏了 ufunc 的类型保真。关闭后,float 输入全程以 float 计算、返回 float。

**练习 2**:`user_overflow_error` 用 `PyErr_SetString` 抛 `OverflowError`,而 `user_evaluation_error` 用 `PyErr_WarnEx` 只发告警并返回 best guess。为什么两者处理力度不同?

**答案**:溢出(overflow)意味着结果已无意义(值超出可表示范围),应中断让上层处理;而求值错误(evaluation error)往往只是某分支算法不稳,Boost 仍能给出一个「最佳估计」。SciPy 选择前者直接抛、后者告警后继续,兼顾了安全性与可用性。

---

### 4.4 Cephes、cdflib、specfun:三套遗留库的归宿

#### 4.4.1 概念说明

除了 xsf 与 Boost,后端版图里还有三套「遗留(legacy)」库,它们各有来历:

- **Cephes**:Stephen L. Moshier 编写的经典 C 数学函数库(可追溯到 1980 年代),曾是 SciPy 特殊函数的主力。它**只提供 double 精度**、C 接口、且历史上有些函数会**静默把 double 参数截断成 int**。
- **cdflib**:一套**概率分布 CDF/分位数**专用库,从 Netlib 上的 Fortran 代码重写为 C,覆盖 F、非中心 F、t、非中心 t、卡方等分布的累积分布与反函数,用 Bus-Dekker 求零算法。
- **specfun**:Shanjie Zhang & Jianming Jin 编写的特殊函数 Fortran 库(常被称为「Zhang & Jin」),负责一些零点计算、Mathieu 函数、球面波函数等。**它的 C++ 移植版已被折叠进 xsf**(`xsf::specfun` 命名空间)。

这三者的共同命运是:**正在被 xsf 吸收或替代**。Cephes 的算法进了 `xsf/cephes/`;specfun 进了 `xsf/specfun/`;cdflib 则仍以独立静态库形式存在,服务于少数非中心分布的反函数。

#### 4.4.2 核心流程

三套库的接入方式各不同:

1. **Cephes(经 `_legacy.pxd`)**:为那些「历史上静默截断 double 成 int」的函数(如 `bdtr`、`kn`、`yn`、`smirnov`、`expn`)提供 `_unsafe` 包装。包装在截断前先检查「截断是否丢失信息」,若丢失则发 `RuntimeWarning`,再转发到 `cephes_*_wrap`(这些 wrap 又来自 xsf 的 `xsf/cephes/*`)。同时,这些函数通常还**并存**一条 xsf 新路径(如 `bdtr` 的 `cephes_bdtr_wrap`),由类型分发二选一。
2. **cdflib(经 `_cdflib_wrappers.pxd`)**:`cdflib.c` 编译成静态库 `cdflib_lib`,函数声明在 `cdflib.h`,Cython 头 `_cdflib_wrappers.pxd` 把它们暴露给 `_ufuncs` 链接调用。仅 `fdtridfd`、`ncfdtridfd`、`ncfdtridfn`、`stdtridf` 这 4 个函数用到。
3. **specfun(经 `_specfun.pyx` 直连 xsf)**:`_specfun.pyx` 直接 `cimport` xsf 里的 `xsf::specfun::` 函数(以及 `xsf::airyzo`、`xsf::fcszo` 等),供 `_basic.py` 的序列型函数(零点、Mathieu 等,见 u4-l2)调用。**它不经过 `functions.json`**。

#### 4.4.3 源码精读

先看 **Cephes 遗留路径**。`_legacy.pxd` 的文档字符串直白说明了它的存在理由——为「历史上静默截断」提供带告警的包装:

[_legacy.pxd:1-8](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_legacy.pxd#L1-L8) —— `_legacy.pxd` 用途:许多 SciPy 特殊函数原本会静默把 double 截成 int,这里手动定义这些 `_unsafe` 包装。

`_legacy.pxd` 先 `cimport` 了来自 `xsf_wrappers.h` 的 Cephes 包装函数——这再次证明「Cephes 已在 xsf 伞下」:

[_legacy.pxd:15-29](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_legacy.pxd#L15-L29) —— `cdef extern from "xsf_wrappers.h"` 声明 `cephes_bdtr_wrap`、`cephes_smirnov_wrap` 等:遗留路径的底层 Cephes 函数,正是由 xsf 包装层提供。

典型的 `_unsafe` 包装 `bdtr_unsafe`:先发弃用/截断告警,做 NaN/inf 检查,再把 `n` 强制 `<int>` 截断后转发到 `cephes_bdtr_wrap`:

[_legacy.pxd:59-65](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_legacy.pxd#L59-L65) —— `bdtr_unsafe`:发 `_legacy_deprecation` 告警,检查 `n` 是否 NaN/inf,否则 `cephes_bdtr_wrap(k, <int>n, p)`(注意 `<int>n` 是有损截断)。

再看 **cdflib**。`cdflib.h` 顶部注释交代了它的血统与覆盖范围:

[cdflib.h:1-28](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cdflib.h#L1-L28) —— cdflib 是 Netlib 上 Fortran 代码的 C 重写,提供 Beta/Binomial/卡方/非中心卡方/F/非中心 F/Gamma/负二项/正态/Poisson/Student's t 的 CDF 与反函数,用 TOMS 算法与 Bus-Dekker 求零。

它在构建侧被编成静态库并链接给 `_ufuncs`:

[meson.build:26-30](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L26-L30) —— `cdflib_lib = static_library('cdflib', 'cdflib.c', ...)`:cdflib 编译为静态库。

[meson.build:100-108](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L100-L108) —— `_ufuncs` 扩展模块的依赖含 `xsf_dep`、`np_dep`,并 `link_with: cdflib_lib`:所以 4 个 cdflib 函数随 `_ufuncs` 提供。

对比之下,`_ufuncs_cxx`(Boost 路径)依赖的是 `boost_math_dep`:

[meson.build:134-144](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L134-L144) —— `_ufuncs_cxx`:`dependencies: [boost_math_dep, xsf_dep, np_dep, ellint_dep]`,且 `cpp_args` 含 `-DBOOST_MATH_STANDALONE=1`(用独立的 Boost.Math,不拉整个 Boost)。C 与 C++ 后端就这样被拆进两个扩展模块,隔离各自的重编译成本(见 u3-l3)。

最后看 **specfun** 的归宿——它的 C++ 移植已在 xsf 里,`_specfun.pyx` 直接从 xsf 头取用,不再是一个独立「库」:

[_specfun.pyx:1-21](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_specfun.pyx#L1-L21) —— `_specfun.pyx` 从 `<xsf/airy.h>`、`<xsf/fresnel.h>`、`<xsf/specfun/specfun.h>` 等 xsf 头 `cimport` `xsf::airyzo`、`xsf::specfun::...`:specfun 已是 xsf 的子命名空间,本文件只是它的 Cython 出口。

#### 4.4.4 代码实践

**实践目标**:追踪一条 Cephes 遗留路径的完整调用链,并验证 cdflib 后端函数可用。

**操作步骤**:

1. 在 `functions.json` 中找 `bdtr`(第 25 行)与 `kn`(第 304 行),确认它们都同时声明了 `_legacy.pxd` 与 `xsf_wrappers.h` 两个来源。
2. 打开 `_legacy.pxd` 第 59-65 行(`bdtr_unsafe`)与第 110-114 行(`kn_unsafe`),看清「告警 → NaN 检查 → `<int>` 截断 → 转发 `cephes_*_wrap` / `special_cyl_bessel_k_int`」的四步模式。
3. 在 Python 验证 cdflib 后端的 `stdtridf`(非中心 t 分布按 df 求逆,头文件 `_cdflib_wrappers.pxd`)可调用:

```python
import scipy.special as sc
print(sc.stdtridf(0.4, 0.7, 5))   # 一个有限实数即可
```

**需要观察的现象**:`bdtr`/`kn` 的 JSON 声明里 `_legacy.pxd` 与 `xsf_wrappers.h` 并存;`stdtridf` 返回一个实数。

**预期结果**:`bdtr` 的两来源并存印证「新旧后端共存、按类型分发」;`stdtridf` 正常返回浮点数(具体值待本地验证)。

#### 4.4.5 小练习与答案

**练习 1**:`bdtr` 的 JSON 声明里既有 `_legacy.pxd`(`bdtr_unsafe`,`ddd->d`)又有 `xsf_wrappers.h`(`cephes_bdtr_wrap`,`dpd->d`)。它们分别处理什么输入?为什么要并存?

**答案**:`ddd->d` 里的三个 `d` 表示「三个参数都按 double 接收」,中间的 `n`(试验次数)会被 `<int>` 截断——这保留了 SciPy 早期「允许传 double 当 int」的旧行为(`bdtr_unsafe`);`dpd->d` 里的 `p` 表示中间参数按指针宽度整数接收,无需截断,是更安全的新路径。两者并存,是为了既兼容历史调用(发告警),又对正确类型的输入走高效安全路径——这正是 u3-l1 讲的「多内核分发」。

**练习 2**:specfun 在本目录里既没有独立的 `.c`/`.cpp` 源文件,也不出现在 `functions.json` 里。那它的算法代码到底在哪、被谁调用?

**答案**:specfun 的 C++ 移植版已被折叠进 xsf,放在 `xsf/specfun/` 子目录、归于 `xsf::specfun` 命名空间(由 Meson 子项目提供,本检出不可见)。它被 `_specfun.pyx` 直接 `cimport`(从 `<xsf/specfun/specfun.h>` 等),再供 `_basic.py` 的序列型函数(零点、Mathieu 等)调用。它**不经过 `functions.json`**,因为它是序列函数而非逐元素 ufunc。

---

## 5. 综合实践

**综合任务:绘制 `scipy.special` 的后端版图并解释 `betainc` 的后端选择。**

把本讲四个模块串起来,完成下面三件事:

1. **普查后端分布**:在 `scipy/special` 目录运行 4.1.4 的统计命令,得到 xsf / Boost / legacy / cdflib 四类头文件字段各自的计数。画一张简单的占比表,体会「xsf 一家独大、Boost 次之、legacy/cdflib 是长尾」的格局。

2. **追一个函数的全链路**:以 `betainc` 为例,从 Python 一路追到 C++ 内核,填写下表(参考答案见后):

   | 层 | 位置 | 内容 |
   | --- | --- | --- |
   | Python 调用 | `sc.betainc(a,b,x)` | 命名空间里的 ufunc |
   | JSON 声明 | `functions.json` 第 67-72 行 | 头文件 `boost_special_functions.h++`,内核 `ibeta_float`/`ibeta_double` |
   | 内核实现 | `boost_special_functions.h` 第 71-145 行 | `ibeta_wrap` 模板 + float/double 实例化 |
   | 策略 | `boost_special_functions.h` 第 18-22 行 | `SpecialPolicy` 关闭类型提升 |
   | 错误桥接 | `boost_special_functions.h` 第 36-69 行 | `user_*_error` 获取 GIL 发 Python 信号 |
   | 编译归宿 | `meson.build` 第 134-144 行 | `_ufuncs_cxx` 扩展模块,依赖 `boost_math_dep` |

3. **回答关键问题:为什么 `betainc` 选 Boost 而非 Cephes?** 结合源码给出基于证据的说明,要点应包括:

   - **证据 A(双精度内核)**:Boost 版提供了 `ibeta_float`(`fff->f`)与 `ibeta_double`(`ddd->d`)双内核(见 4.3.3),能保住 float32 类型环;而本代码库里 Cephes 的强项是不完全 Gamma(见 `xsf_wrappers.cpp` 第 46-61 行包含的 `<xsf/cephes/igam.h>` 等),**未提供**同等的不完全 Beta 双内核。
   - **证据 B(策略化错误/极限语义)**:Boost 的 `SpecialPolicy` 能把 domain/overflow/underflow 精细映射成 `SF_ERROR_*`(见 `ibeta_wrap` 的 try/catch),并支持 SciPy 想要的「极限情形返回特定值」语义(`ibeta_wrap` 里对 `(a,b)→(0,0)` 等返回 NaN/0/1 的处理);Cephes 的老接口难以表达这些分布极限语义。
   - **证据 C(精度)**:Boost.Math 以高精度和跨平台一致性著称,不完全 Beta 这类对数值稳定性敏感的函数,Boost 实现更可靠。
   - **结论**:`betainc` 选 Boost,是为了同时拿到「float32 支持 + 精细错误/极限语义 + 高精度」,这是 Cephes 旧实现给不了的。这个选择不是孤例——所有 `beta*` 系列(`betaincc`/`betaincinv`/`betainccinv`)、以及多数 `*_ppf` 分位数函数都走了 Boost(可自行在 `functions.json` 里验证)。

> 把上述三步整理成一页笔记,你就掌握了「从一条 ufunc 声明反查它用了哪个后端、为什么用这个后端」的方法论——这正是阅读 `scipy.special` 源码时最常需要的技能。

## 6. 本讲小结

- `scipy.special` 的数值内核由**五套 C/C++ 库**拼装:**xsf**(自研现代 C++,收敛终点)、**Boost.Math**(高精度重型库)、**Cephes**(经典 C 遗留)、**cdflib**(概率分布 CDF/分位数)、**specfun**(Zhang & Jin,已并入 xsf)。
- **后端调度是声明式的**:由 `functions.json` 的「头文件」字段决定,看头文件名末尾是否有 `++` 即可判 C/C++(`.h++` ⇒ Boost C++ ⇒ `_ufuncs_cxx`;`.h`/`.pxd` ⇒ C ⇒ `_ufuncs`)。
- 当前有**两条注册路径**:JSON 生成路径(`functions.json`,约 128 条)与 C++ 直注册路径(`_special_ufuncs.cpp`,约 189 个 `special_ufuncs`)。`_generate_pyx.py` 用 `if f not in special_ufuncs` 闸门避免重复生成;`erf`/`airy` 等明星函数已迁出 JSON。
- **xsf 是收敛终点**:它既自研新实现,又把 Cephes(`xsf/cephes/`)、specfun(`xsf/specfun/`)折叠进来;`xsf_wrappers.*` 用 `extern "C"` + `to_complex/to_ccomplex` 把它桥接给 C/ufunc 层。
- **Boost 后端靠 policy 改造**:`SpecialPolicy` 关闭类型提升(保 float32)、用 `user_*_error` 在 GIL 内把 C++ 异常翻译成 Python 告警/异常,并提供 float/double 双内核。
- **遗留库各有归宿**:Cephes 经 `_legacy.pxd` 的 `_unsafe` 包装(带截断告警)并存于 xsf 新路径;cdflib 编成静态库 `cdflib_lib` 仅服务 4 个非中心分布反函数;specfun 直连 xsf 供序列函数使用,不进 JSON。

## 7. 下一步学习建议

本讲把「后端版图」讲完了,接下来可以按兴趣选三个方向深挖:

1. **沿「C++ 后端实现」继续下钻**:本讲的 `xsf_wrappers.*` 只是包装层,真正的算法在 xsf 库(构建期拉取的子项目)里。如果你对某个具体函数的数值算法感兴趣(比如 Bessel 函数的 Amos 算法、Airy 函数的渐近展开),可以去看 xsf 仓库的 `<xsf/amos.h>`、`<xsf/airy.h>` 等头文件。对应的讲义是 **u8-l1(xsf 与 xsf_wrappers)** 和 **u8-l2(Boost.Math 集成)**,它们从「实现细节」角度补全本讲从「版图」角度给出的概览。

2. **沿「错误处理」深入**:本讲多次提到 `sf_error`、`PyGILState_Ensure`、`SpecialFunctionWarning`,但都浅尝辄止。C 内核检测到数值错误后**如何隔着 GIL 触发 Python 告警**,是 special 工程的精华之一,详见 **u7-l1(sf_error 的 C→Python 桥)** 和 **u7-l2(_ufuncs_extra_code.pxi 的 seterr/errstate)**。

3. **沿「纯 Python 包装层」横向展开**:本讲关注 C/C++ 内核;但这些内核之上还有一层纯 Python 函数(`_basic.py` 的组合数学、零点序列,`_logsumexp.py` 等),它们有的复用本讲的内核,有的纯 Python 实现。详见 **u4-l1 ~ u4-l4**。

建议的阅读顺序:先 u4(看清 Python 层如何调用本讲的内核),再 u8(下钻到 C++ 实现细节),最后 u7(把错误处理闭环)。这样就从「版图」走到「实现」,再走到「健壮性」,形成对 special 的完整理解。
