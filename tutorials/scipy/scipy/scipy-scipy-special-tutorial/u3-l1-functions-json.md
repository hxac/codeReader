# functions.json:声明式 ufunc 签名与类型码

## 1. 本讲目标

本讲是 **U3 代码生成管线**单元的第一讲,进入 `scipy.special` 的「工程心脏」——声明式代码生成。

学完本讲后,你应当能够:

1. 打开 `functions.json`,逐层读懂任意一条函数声明,说出「函数名 → 头文件 → 内核名 → 类型签名」四层结构各自含义。
2. 熟练使用单字符类型码(`f/d/g/F/D/G/i/l/p`)和 `input*output->retval` 签名语法,亲手解析 `dpd->d`、`d*dd->*i`、`ddd->d` 这类签名到底表示几个输入、几个输出、返回值如何处理。
3. 理解「多内核分发」的两套机制:一种是显式列出 float+double 双内核(如 `betainc`),另一种是生成器自动派生的类型转换变体(`iter_variants`),并明白它们为什么这样设计。

本讲只精读两个文件:`functions.json`(声明)与 `_generate_pyx.py`(消费声明的生成器)。生成器如何把声明编译成 `.pyx` 源码,留待下一讲 u3-l2。

---

## 2. 前置知识

本讲建立在 u1-l2(目录分层)、u1-l3(构建管线)与 u2-l1(ufunc 类型系统)之上,这里做最简回顾:

- **三层架构回顾**:`scipy.special` 自上而下是 Python 包装层(`.py`)→ Cython 胶水层(`.pyx/.pxd/.pxi`)→ C/C++ 数学内核层(`.c/.cpp/.h`)。绝大多数函数最终是一个 **NumPy ufunc**(详见 u2-l1)。
- **ufunc 与 loop(类型环)**:一个 ufunc 内部挂载多条「内层循环」(loop),每条 loop 对应一组输入→输出类型组合,例如 `erf` 挂了 `f->f`、`d->d`、`F->F`、`D->D` 四环。运行时 NumPy 按输入 `dtype` 自动选择匹配的 loop。
- **代码生成的位置**(u1-l3):`_ufuncs.pyx` 这个文件**不在源码目录里**,它是构建时由 `_generate_pyx.py` 读取 `functions.json` 生成的。本讲回答:那份「数据」`functions.json` 到底长什么样、生成器如何读懂它。
- **声明式生成**:与其手写几百个结构相似的 `PyUFunc_FromFuncAndData(...)` 注册调用,作者把「哪个函数、来自哪个头文件、什么类型签名」抽成一张 JSON 表,让程序去生成重复代码。这是本模块控制复杂度的核心工程手段。

> 一个直觉类比:`functions.json` 像「菜单」,`_generate_pyx.py` 像「厨师」,生成出的 `_ufuncs.pyx` 像「按菜单现做出来的菜」。本讲先把「菜单」的语法彻底搞懂。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
| --- | --- | --- |
| `scipy/special/functions.json` | **声明表**:每个 ufunc 名 → 它的内核来源与类型签名 | 本讲主角,逐条解析其三层结构 |
| `scipy/special/_generate_pyx.py` | **代码生成器**:读取 JSON 并产出 `.pyx` 源码 | 用它的类型码字典、签名解析正则、变体派生逻辑来「反向解释」JSON 语法的含义 |
| `scipy/special/orthogonal_eval.pxd` | Cython 头:正交多项式求值的融合类型内核 | 解释 `eval_chebyc[double]` 这种**融合类型特化**语法的来源 |
| `scipy/special/_complexstuff.pxd` | Cython 头:定义 `number_t` 融合类型 | 说明 `[double]` / `[double complex]` 两个特化分支从何而来 |

> 说明:本讲引用的 `bdtr`、`betainc`、`eval_chebyc` 三个条目都真实存在于 `functions.json` 中,行号已逐一核对。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块:**4.1 JSON 声明结构**、**4.2 类型码语法**、**4.3 多内核分发与自动类型转换**。三者层层递进:先看「形状」,再看「字母」,最后看「同义多写与自动补全」。

### 4.1 JSON 声明结构:函数名 → 头文件 → 内核名 → 签名

#### 4.1.1 概念说明

`functions.json` 的顶层是一个 JSON 对象,**键是 ufunc 的 Python 名字**(也就是你在 `scipy.special` 命名空间里调用的名字,如 `bdtr`、`betainc`、`eval_chebyc`),**值又是一个对象**,描述「这个 ufunc 的内核来自哪里、叫什么、什么类型」。

关键在于这是一个**三层嵌套映射**:

```
<ufunc 名字>           ← 顶层键:Python 侧可见的函数名
  └─ <头文件>          ← 中层键:内核声明在哪个头文件里(决定后端)
       └─ <内核名> : <签名>   ← 内层键值:真正的 C/C++/Cython 函数名 与 类型签名
```

为什么要分三层?

- **顶层(函数名)**:对外契约稳定。用户调用 `special.bdtr(...)`,这个名字不能随便改。
- **中层(头文件)**:把「接口名」与「实现来源」解耦。同一个 ufunc 可以同时挂多个头文件(多条实现路径),运行时按类型自动挑选。头文件名还携带「语言」信息:`.h` 是 C 头、`.h++` 是 C++ 头、`.pxd` 是 Cython 头。
- **内层(内核名 + 签名)**:真正落到 C/C++/Cython 层的函数符号,以及它接受/返回什么类型。一个头文件下可以列多个内核(同一函数的 float 版、double 版等)。

这种「接口名 → 多个实现 → 各自的类型签名」结构,正是 ufunc **多 loop 分发**的声明式写法:你把所有候选内核和它们的类型都列出来,生成器和 NumPy 的类型解析机制帮你串起来。

#### 4.1.2 核心流程

生成器消费一条声明的流程可以这样描述(伪代码):

```
for ufunc_name, header_map in functions.json.items():
    if ufunc_name in special_ufuncs:        # 该名字走更新的 C++ 注册路径
        continue                            # 见 4.3.4
    for header, kernels in header_map.items():
        for kernel_name, signature in kernels.items():
            inarg, outarg, ret = parse(signature)   # 见 4.2
            语言 = 'C++' if header.endswith('.h++') else 'C/Cython'
            记录 (kernel_name, inarg, outarg, ret, header)
    把所有候选内核 → 生成为一个 ufunc 注册调用
```

要点:

1. **一个 ufunc 可以挂多个头文件**(多条实现路径)。例如 `bdtr` 同时挂在 `_legacy.pxd`(Cython 旧实现)和 `xsf_wrappers.h`(Cephes/xsf 新实现)下。
2. **一个头文件下可以列多个内核**(多个类型版本)。例如 `betainc` 在 `boost_special_functions.h++` 下同时列了 `ibeta_float` 和 `ibeta_double`。
3. **生成器只认名字不在 `special_ufuncs` 列表里的条目**——其余名字(如 `airy`、`agm`)走另一条更新的 C++ 注册路径,见 4.3.4。

#### 4.1.3 源码精读

先看生成器顶部 docstring 对 JSON 结构的总述:

[`_generate_pyx.py` L11-L20] — 声明签名语法的权威定义(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L11-L20)

```python
The functions' signatures are contained in 'functions.json'.
The syntax for a function signature is

    <function>:       <name> ':' <input> '*' <output>
                        '->' <retval> '*' <ignored_retval>
    <input>:          <typecode>*
    <output>:         <typecode>*
    ...
    <headers>:        <header_name> [',' <header_name>]*
```

这段把「函数名 + 头文件 + 签名」的三段式关系讲清楚了:多个内核可共用一个头文件,也可每个内核独占一个头文件。

docstring 还点明头文件可以是 C 头或 Cython pxd:

[`_generate_pyx.py` L44-L46] — `.pxd` 与 `.h` 并存(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L44-L46)

> "There should be either a single header that contains all of the kernel functions listed, or there should be one header for each kernel function. Cython pxd files are allowed in addition to .h files."

以及 C++ 的标记方式(`++` 后缀):

[`_generate_pyx.py` L51-L52] — C++ 头用 `++` 后缀(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L51-L52)

> "Function coming from C++ should have ``++`` appended to the name of the header."

现在看真实条目。**`bdtr`(二项分布 CDF)挂两个头文件**:

[`functions.json` L25-L32] — bdtr 的双层来源(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/functions.json#L25-L32)

```json
"bdtr": {
    "_legacy.pxd": {
        "bdtr_unsafe": "ddd->d"
    },
    "xsf_wrappers.h": {
        "cephes_bdtr_wrap": "dpd->d"
    }
}
```

读法:

- 顶层 `bdtr` = ufunc 的 Python 名。
- 中层有**两个**头文件:`_legacy.pxd`(Cython 头,旧实现)和 `xsf_wrappers.h`(C 头,新实现)。
- 内层各自一个内核:`bdtr_unsafe`(签名 `ddd->d`,注意 `_unsafe` 是 `_legacy.pxd` 内核的统一命名约定,标识「遗留路径」)与 `cephes_bdtr_wrap`(签名 `dpd->d`)。
- 两条实现的输入类型不同(一个全 double,一个中间参数是整数 `p`),所以它们服务不同的输入 dtype 组合,共存而非冲突(详见 4.3)。

**`betainc`(不完全 Beta 函数)只挂一个 C++ 头文件,但下面列两个内核**:

[`functions.json` L67-L72] — betainc 的 float+double 双内核(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/functions.json#L67-L72)

```json
"betainc": {
    "boost_special_functions.h++": {
        "ibeta_float": "fff->f",
        "ibeta_double": "ddd->d"
    }
}
```

读法:头文件 `boost_special_functions.h++` 末尾的 `++` 表明这是 **C++ 头**(Boost.Math);它下面列了两个内核——float 版 `ibeta_float`(`fff->f`)与 double 版 `ibeta_double`(`ddd->d`),各自服务 float32 / float64 输入。

**`eval_chebyc`(Chebyshev 多项式求值)用 Cython 融合类型**:

[`functions.json` L165-L170] — eval_chebyc 的融合类型特化(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/functions.json#L165-L170)

```json
"eval_chebyc": {
    "orthogonal_eval.pxd": {
        "eval_chebyc[double complex]": "dD->D",
        "eval_chebyc[double]": "dd->d",
        "eval_chebyc_l": "pd->d"
    }
}
```

这里的 `[double]` / `[double complex]` 是 **Cython 融合类型(fused type)的特化写法**——同一个 Cython 泛型函数 `eval_chebyc` 编译出两个具体版本,分别处理实数和复数点。生成器 docstring 专门提到这种写法:

[`_generate_pyx.py` L48-L49] — 融合类型要用特化名(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L48-L49)

> "Cython functions may use fused types, but the names in the list should be the specialized ones, such as 'somefunc[float]'."

而 `eval_chebyc_l`(签名 `pd->d`)是另一个独立函数:`_l` 后缀表示「large order」大阶版本,用 `p`(Py_ssize_t)而非 `d`/`i` 来表示多项式阶数,避免高阶时 32 位整数溢出。`pd->d` 读作:阶数用 `p`(指针宽度整数)、求值点用 `d`(double)、返回 `d`。

#### 4.1.4 代码实践

**实践目标**:亲手解析三条真实声明,把四层结构填进表格。

**操作步骤**:

1. 打开 `scipy/special/functions.json`,定位 `bdtr`(约 L25)、`betainc`(约 L67)、`eval_chebyc`(约 L165)三条。
2. 对每一条,在纸上(或文本里)画出「顶层名 / 中层头文件 / 内层内核名 / 内层签名」四列。
3. 判断每条**是否用了 C++**:看中层头文件名是否以 `++` 结尾。

**需要观察的现象 / 预期结果**:

| ufunc 名 | 头文件 | 内核名 | 签名 | 是否 C++ |
| --- | --- | --- | --- | --- |
| `bdtr` | `_legacy.pxd` / `xsf_wrappers.h` | `bdtr_unsafe` / `cephes_bdtr_wrap` | `ddd->d` / `dpd->d` | 否(Cython + C) |
| `betainc` | `boost_special_functions.h++` | `ibeta_float` / `ibeta_double` | `fff->f` / `ddd->d` | **是**(`++` 标记) |
| `eval_chebyc` | `orthogonal_eval.pxd` | `eval_chebyc[double complex]` / `eval_chebyc[double]` / `eval_chebyc_l` | `dD->D` / `dd->d` / `pd->d` | 否(Cython) |

> 因此三条里**只有 `betainc` 用到了 C++**(Boost.Math),判据就是头文件名的 `++` 后缀。`bdtr` 是 Cython+C,`eval_chebyc` 是纯 Cython。

> 若无法本地打开文件,可直接对照本讲引用的永久链接片段核对,结论一致。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `bdtr` 要同时挂 `_legacy.pxd` 和 `xsf_wrappers.h` 两个头文件,而不是只留一个新的?

**参考答案**:两个内核的输入签名不同(`ddd->d` 全 double vs `dpd->d` 中间参数为整数 `p`),它们服务不同的输入类型组合;同时 `_legacy.pxd` 的 `ddd->d` 因不含整数参数,会被自动派生出 float32 变体(见 4.3.2),从而让 `bdtr` 也能接受 float32 输入。保留两条路径是为了类型覆盖与数值实现的互补,而不是冗余。

**练习 2**:`_legacy.pxd` 下的内核名都带 `_unsafe` 后缀(如 `bdtr_unsafe`、`yn_unsafe`),这个命名约定想传达什么?

**参考答案**:这是一种**有意的命名警示**——`_legacy.pxd` 是「遗留/不安全」实现路径(通常是无类型检查或旧算法的直通包装),`_unsafe` 后缀提醒维护者:这些内核在类型或数值安全性上不如新路径,应优先让新路径(xsf/Cephes/Boost)承担分发,旧路径只作为兜底。

---

### 4.2 类型码语法:单字符类型与 `input*output->retval`

#### 4.2.1 概念说明

签名是一串紧凑的字符,例如 `dpd->d`、`d*dd->*i`、`fff->f`。要读懂它,需要两样东西:

1. **单字符类型码**:每个字符代表一种 C/NumPy 类型。
2. **结构语法**:字符的排列规则——哪里是输入、哪里是输出、`*` 和 `->` 各自的作用。

这套类型码**直接对应 NumPy 的数据类型**,也是 ufunc loop 的「类型环」标识(回顾 u2-l1:`erf.types` 返回 `f->f`、`d->d` 等,就是这些码)。

#### 4.2.2 核心流程

类型码全表(来自生成器的 docstring,也是 C/Cython 层的映射依据):

| 码 | C 类型(C_TYPES) | Cython 类型(CY_TYPES) | NumPy 类型(TYPE_NAMES) | 直觉含义 |
| --- | --- | --- | --- | --- |
| `f` | `npy_float` | `float` | `NPY_FLOAT` | 32 位浮点 |
| `d` | `npy_double` | `double` | `NPY_DOUBLE` | 64 位浮点(默认) |
| `g` | `npy_longdouble` | `long double` | `NPY_LONGDOUBLE` | 扩展精度 |
| `F` | `npy_cfloat` | `float complex` | `NPY_CFLOAT` | 32 位复数 |
| `D` | `npy_cdouble` | `double complex` | `NPY_CDOUBLE` | 64 位复数(默认) |
| `G` | `npy_clongdouble` | `long double complex` | `NPY_CLONGDOUBLE` | 扩展精度复数 |
| `i` | `npy_int` | `int` | `NPY_INT` | 整数(C int) |
| `l` | `npy_long` | `long` | `NPY_LONG` | 长整数 |
| `p` | `npy_intp` | `Py_ssize_t` | `NPY_INTP` | 指针宽度整数(下标/计数专用) |
| `v` | `void` | `void` | — | 仅用于「无返回值」,且不得出现在输出位 |

签名有两种语法形式(由解析正则区分,见 4.2.3):

**形式 A(单输出,经返回值返回)**:`<input>-><retval>`,没有 `*`。

- 例:`d->d` = 一个 double 输入,返回 double。`erf(x)` 即如此。
- 例:`i->i` = 一个 int 输入,返回 int(`_sf_error_test_function`)。

**形式 B(多输出,经指针参数返回)**:`<input>*<output>-><retval>`,有 `*`。

- `*` **左边**是「按值传入」的输入参数;`*` **右边**是「按指针写出」的输出参数。
- `->` 后面是 C 函数的真实返回值,通常是一个 **被丢弃的状态码**,写作 `*<码>`(开头的 `*` 表示「忽略此返回值」)。
- 例:`d*dd->*i` = 一个 double 输入、两个 double 指针输出、一个被忽略的 int 返回值。这正是 `sici(x) -> (si, ci)`:`x` 传入,`Si`/`Ci` 经指针写出,内核返回的 int 状态码被丢弃。

> 为什么多输出要走指针?因为 C 函数只能返回一个值。要让一个 ufunc 一次产出多个数组(如 `sici` 的 `(si, ci)`、`airy` 的四个分量),内核就必须把多余结果写进指针参数;而那个「真正返回的 int」通常是 sf_error 状态码,交给错误处理管线(详见 u7),在 ufunc 层面被忽略。

数学上,可以把 ufunc 的输入/输出关系记作:

\[
\text{ufunc}:\ (\text{in}_1,\dots,\text{in}_m)\ \mapsto\ (\text{out}_1,\dots,\text{out}_n)
\]

形式 A 对应 \(n=1\) 且输出走返回值;形式 B 对应 \(n\ge 1\) 且输出走指针,返回值被丢弃。

#### 4.2.3 源码精读

类型码到三套 C/Cython/NumPy 名字的映射,在生成器里是三张并排的字典:

[`_generate_pyx.py` L339-L375] — CY_TYPES / C_TYPES / TYPE_NAMES 三表(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L339-L375)

```python
CY_TYPES = {'f': 'float', 'd': 'double', 'g': 'long double',
            'F': 'float complex', 'D': 'double complex', 'G': 'long double complex',
            'i': 'int', 'l': 'long', 'p': 'Py_ssize_t', 'v': 'void'}
C_TYPES   = {'f': 'npy_float', 'd': 'npy_double', ... 'p': 'npy_intp', 'v': 'void'}
TYPE_NAMES= {'f': 'NPY_FLOAT', 'd': 'NPY_DOUBLE', ... 'p': 'NPY_INTP'}
```

这三张表分别用于:生成 Cython 端原型(`CY_TYPES`)、生成 C 端函数指针原型(`C_TYPES`)、生成 NumPy ufunc 注册时的类型编号(`TYPE_NAMES`)。**同一个码 `d` 在三个语境下对应三种写法**,这是「声明式」的核心收益——你只需写一个 `d`,生成器替你换成三种正确拼写。

签名的解析由一个正则完成,它就是 4.2.2 所述「形式 A / 形式 B」的代码化身:

[`_generate_pyx.py` L607-L620] — `_parse_signature` 正则解析(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L607-L620)

```python
def _parse_signature(self, sig):
    T = 'fdgFDGilp'
    # 形式 B: input * output -> retval(允许 retval 以 * 开头表示忽略)
    m = re.match(rf"\s*([{T}]*)\s*\*\s*([{T}]*)\s*->\s*([*{T}]*)\s*$", sig)
    if m:
        inarg, outarg, ret = (x.strip() for x in m.groups())
        if ret.count('*') > 1:
            raise ValueError(f"{self.name}: Invalid signature: {sig}")
        return inarg, outarg, ret
    # 形式 A: input -> retval(无 *)
    m = re.match(rf"\s*([{T}]*)\s*->\s*([{T}]?)\s*$", sig)
    if m:
        inarg, ret = (x.strip() for x in m.groups())
        return inarg, "", ret
    raise ValueError(f"{self.name}: Invalid signature: {sig}")
```

读法要点:

- `T = 'fdgFDGilp'` 是合法类型码集合(注意**不含 `v`** 和 `q`;`v` 仅作返回值的「空」占位,`q` 只在 4.3 的整数判别里作为保留字符出现)。
- 先尝试形式 B(必须含 `*`):三段分别是 input / output / retval,retval 段允许以单个 `*` 开头表示「忽略返回值」(`ret.count('*') > 1` 报错,确保最多一个 `*`)。
- 形式 B 不匹配才试形式 A(无 `*`):只有 input 和 retval。
- 都不匹配就抛 `ValueError`——这就是为什么本模块不会出现畸形签名。

`* retval` 的「忽略」语义在分发逻辑里被兑现:

[`_generate_pyx.py` L716-L718] — retval 中 `*` 后的部分被剥离,真实输出 = retval 前 + outarg(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L716-L718)

```python
for func_name, inarg, outarg, ret, header in self.signatures:
    outp = re.sub(r'\*.*', '', ret) + outarg   # 剥掉 "*i",真实输出 = outarg
    ret = ret.replace('*', '')                  # 去掉 *,得到真实返回类型码
```

也就是说,对 `sici` 的 `d*dd->*i`:input=`d`、outarg=`dd`、ret=`*i` → 真实输出 `outp='dd'`(两个 double)、真实返回码 `ret='i'`(int,但在 ufunc 层丢弃)。对照真实条目核对:

[`functions.json` L466-L471] — sici 的多输出签名(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/functions.json#L466-L471)

```json
"sici": {
    "xsf_wrappers.h": {
        "xsf_csici": "D*DD->*i",
        "xsf_sici": "d*dd->*i"
    }
}
```

`xsf_sici: d*dd->*i` = 实数版:1 个 double 输入 `x`,2 个 double 指针输出 `(Si, Ci)`,丢弃 int 状态返回。`xsf_csici: D*DD->*i` = 复数版:1 个 double complex 输入,2 个 double complex 指针输出。两者挂在同一 ufunc `sici` 下,按输入是实/复数自动分发。

最后看「原型生成」,它把签名翻译成 C/Cython 函数指针类型——这解释了为什么输出参数在 C 侧是「指针」:

[`_generate_pyx.py` L622-L636] — `get_prototypes`:输出参数自动加 `*`(指针)(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L622-L636)

```python
c_args  = ([C_TYPES[x] for x in inarg]              # 输入:按值
           + [C_TYPES[x] + ' *' for x in outarg])    # 输出:加 ' *' 变指针
...
c_proto = f"{C_TYPES[ret]} (*)({', '.join(c_args)})"
```

可见 `outarg` 里的每个码在 C 原型里都被加上 `' *'`,正好对应 4.2.2 说的「输出走指针」。

#### 4.2.4 代码实践

**实践目标**:把抽象签名翻译成「几个输入、几个输出、什么类型」的人话描述。

**操作步骤**:

1. 取以下五条签名,逐条用 4.2.2 的规则手工解析:
   - `d->d`(erf 类)
   - `dpd->d`(cephes_bdtr_wrap)
   - `ddd->d`(bdtr_unsafe)
   - `d*dd->*i`(xsf_sici)
   - `ddD->D`(eval_gegenbauer[double complex])
2. 对每条,写出:输入个数与类型、输出个数与类型、返回值是否被忽略。

**需要观察的现象 / 预期结果**(可对照 4.2.3 的源码逻辑核对):

| 签名 | 输入 | 输出 | 返回值 |
| --- | --- | --- | --- |
| `d->d` | 1×double | 1×double(经返回值) | 用作输出,不忽略 |
| `dpd->d` | double, npy_intp, double | 1×double(经返回值) | 用作输出 |
| `ddd->d` | 3×double | 1×double(经返回值) | 用作输出 |
| `d*dd->*i` | 1×double | 2×double(经指针) | int,被忽略 |
| `ddD->D` | double, double, double complex | 1×double complex(经返回值) | 用作输出 |

**预期结果**:你能仅凭字符串准确说出每条的输入输出数;尤其能区分 `d->d`(单值返回)与 `d*dd->*i`(指针多输出 + 丢弃状态码)。

> 若想验证翻译是否正确,可在已安装 SciPy 的环境里 `python -c "import scipy.special as sc; print(sc.erf.types, sc.sici.types)"`,把 `.types` 输出与本练习对照(注意 `.types` 用的是 NumPy 的类型简写如 `f->f`、`dd->dd`,与本讲的码一致)。「待本地验证」你是否能跑通该命令。

#### 4.2.5 小练习与答案

**练习 1**:`d*dd->*i` 里那个被忽略的 `i` 返回值通常是什么?为什么 ufunc 层要丢弃它?

**参考答案**:它通常是内核通过返回值上报的 **sf_error 状态码**(成功/奇异/下溢/溢出/定义域等,详见 u7)。ufunc 的输出契约是「输入数组 → 输出数组」,这个状态码不是数值结果,因此 ufunc 层丢弃它;但生成器会把 ufunc 内层的硬件浮点异常转成 sf_error 信号(见 u7-l2),从而让错误处理在 Python 侧仍可被 `seterr/errstate` 捕获。

**练习 2**:为什么 `bdtr` 的 xsf 内核用 `p`(npy_intp)来表示计数参数 `n`,而 legacy 内核用 `d`(double)?

**参考答案**:计数 `n` 在语义上是整数,用指针宽度整数 `p`(`Py_ssize_t`/`npy_intp`)既能精确表达「个数」语义,又能在 64 位平台上支持很大的 `n` 而不溢出,且作为整数参数参与 ufunc 类型解析(见 4.3.2,含整数参数的内核不会被自动派生 float32 变体,避免数值语义错乱)。legacy 路径用 `d` 是历史做法——把 `n` 当 double 传,再在内部转回整数,不够严谨,这也是它被标为 `_unsafe` 的原因之一。

---

### 4.3 多内核分发与自动类型转换

#### 4.3.1 概念说明

一个 ufunc 能同时接受 float32、float64、复数等多种输入,是因为它内部挂了多条 loop(类型环)。在 `functions.json` 里,这些 loop 来自两个来源:

1. **显式多内核**:你手动在同一个头文件下列出多个内核(如 `betainc` 列 `ibeta_float` + `ibeta_double`)。每个内核对接一种或几种类型。
2. **自动派生变体**:生成器 `iter_variants` 会基于你写的那条签名,自动「复制」出额外的类型变体(如把 `d->d` 自动补出 `f->f`),免去你手写重复条目。

理解这两者,就理解了「为什么 `betainc` 要显式写两个内核,而 `erf` 只写 `d->d` 一个就够」。

#### 4.3.2 核心流程

**(a) 显式多内核的分发规则**

docstring 给出权威规则:

[`_generate_pyx.py` L36-L39] — 列在前的内核优先匹配(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L36-L39)

> "If multiple kernel functions are given for a single ufunc, the one which is used is determined by the standard ufunc mechanism. Kernel functions that are listed first are also matched first against the ufunc input types, so functions listed earlier take precedence."

即:多个内核都能匹配同一组输入类型时,**列在前面的优先**;最终再按 NumPy 标准的「类型提升」规则解析。生成器还会按一个稳定的「类型优先级」(cast_order)对所有变体排序,保证更「精确」的类型先匹配。

**(b) 自动派生的转换变体**

生成器会自动补出两类变体(见 `iter_variants`):

1. **总是把 `i` → `l`**:凡是签名里的 `int`,都额外生成一份用 `long` 的版本(64 位平台上 long 更常用)。
2. **当且仅当签名不含任何整数参数(`i/l/q/p`)时**,额外把 `d→f`、`D→F` 派生出 float32 / float complex 变体,指向**同一个**内核函数(运行时 float 会被转成 double 调用)。

第二条有一个重要的工程原因,docstring 与代码都点明了:含整数参数的 ufunc 若自动派生 float32 变体,会触发 NumPy 的 dtype 选择 bug(整数数组 + float 标量时可能错选 dtype,见 gh-4895),因此**含整数参数时干脆不派生 float32 变体**。

**(c) 危险下cast 的禁止**

并非所有类型转换都被允许。`DANGEROUS_DOWNCAST` 列出会丢失信息(返回 NaN)的转换对(如 `d→i`、`D→f`、`p→i`),这些不会被自动派生,避免静默给出错误结果。

**(d) `special_ufuncs` 名单:另一条注册路径**

`functions.json` **并不覆盖全部 ufunc**。像 `airy`、`agm`、`binom`、`logit` 这些名字不在 `functions.json` 里——它们登记在生成器顶部的 `special_ufuncs` 名单中,走的是更新的 **纯 C++ 注册路径**(`_special_ufuncs.cpp`,详见 u8-l3)。`main()` 里有一道闸门:凡 `functions.json` 中的名字也出现在 `special_ufuncs` 名单里,就跳过不重复生成。

#### 4.3.3 源码精读

先看自动派生的核心函数 `iter_variants`:

[`_generate_pyx.py` L546-L589] — `iter_variants`:自动派生类型变体(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L546-L589)

```python
def iter_variants(inputs, outputs):
    maps = [
        # always use long instead of int (more common type on 64-bit)
        ('i', 'l'),
    ]
    # float32-preserving signatures
    if not ('i' in inputs or 'l' in inputs or 'q' in inputs or 'p' in inputs):
        # Don't add float32 versions of ufuncs with integer arguments, as this
        # can lead to incorrect dtype selection ...
        maps = maps + [(a + 'dD', b + 'fF') for a, b in maps]
    # do the replacements
    for src, dst in maps:
        new_inputs = inputs.replace(...); new_outputs = outputs.replace(...)
        yield new_inputs, new_outputs
```

读法:

- `('i','l')` 这条映射始终生效,所以 `int` 输入总有 `long` 变体。
- 仅当输入**不含** `i/l/q/p` 时,追加 `('ldD','lfF')` 这条映射——等价于把签名里的 `d→f`、`D→F`,即派生出 float32 / float complex 变体,且复用同一个内核函数。
- 因此一条 `d->d` 内核,实际产出 `d->d` 与 `f->f` 两环;而 `dpd->d`(含 `p`)只产出自身,不派生 float32。

这就解释了两个对照案例:

- **`erf` 写 `d->d` 就够**:无整数参数,自动派生出 `f->f`,于是 float32 输入也能用(只是内部转 double 计算)。
- **`betainc` 显式写 `ibeta_float: fff->f` 与 `ibeta_double: ddd->d`**:虽然 `ddd->d` 也会自动派生 `fff->f`(指向 `ibeta_double`),但作者**额外**显式提供 `ibeta_float`,让 float32 输入调用**专门为 float 优化的 Boost 内核**,而不是转 double 再算——这是为了 float32 的性能/精度专门维护一条实现。由于「列在前者优先」,`ibeta_float` 排在 `ibeta_double` 之前,float32 输入优先命中它。

接着看「去重 + 排序」的分发装配逻辑(它在 `_get_signatures_and_loops` 里):

[`_generate_pyx.py` L692-L738] — 装配 loop:先加显式内核,再加派生变体,按 cast_order 稳定排序(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L692-L738)

```python
def _get_signatures_and_loops(self, all_loops):
    ...
    seen = set()
    def add_variant(func_name, inarg, outarg, ret, inp, outp):
        if inp in seen:
            return                # 同一组输入类型,只保留第一个
        seen.add(inp)
        ...
    # First add base variants(显式内核优先登记)
    for func_name, inarg, outarg, ret, header in self.signatures:
        ...
        inp, outp = list(iter_variants(inarg, outp))[0]   # 只取原始那条
        add_variant(...)
    # Then the supplementary ones(再补派生变体)
    for func_name, inarg, outarg, ret, header in self.signatures:
        ...
        for inp, outp in iter_variants(inarg, outp):
            add_variant(...)
    # Then sort variants to input argument cast order(稳定排序)
    variants.sort(key=lambda v: cast_order(v[2]))
    return variants, inarg_num, outarg_num
```

读法:

- `seen` 集合按「输入类型串」去重。显式内核的原始签名先登记,因此当 `ibeta_double` 的派生 `fff->f` 变体后来想登记时,`ibeta_float` 已经占了 `fff` 这一组,被跳过——于是 float32 输入命中**专用的** `ibeta_float`,而非转 double 的退化版本。这正是显式 float 内核的价值。
- `cast_order` 定义类型优先级:

[`_generate_pyx.py` L382-L383] — cast_order:类型优先级序列(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L382-L383)

```python
def cast_order(c):
    return ['ilpfdgFDG'.index(x) for x in c]
```

排序键是每个码在 `'ilpfdgFDG'` 中的下标,整数(i/l/p)在前、实浮点(f/d/g)在中、复数(F/D/G)在后。配合 Python 稳定排序,「列在前面的同类型内核仍优先」这一规则得以保留。

危险下cast 的禁止表:

[`_generate_pyx.py` L386-L397] — DANGEROUS_DOWNCAST(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L386-L397)

```python
# These downcasts will cause the function to return NaNs, unless the
# values happen to coincide exactly.
DANGEROUS_DOWNCAST = {
    ('F', 'i'), ('F', 'l'), ... ('F', 'f'), ('F', 'd'), ('F', 'g'),
    ('D', 'i'), ... ('d', 'i'), ('d', 'l'), ('d', 'p'),
    ('p', 'l'), ('p', 'i'), ('l', 'i'),
}
```

这张表在生成 loop 时被用来判断「这个变体是否需要插入显式的相等性检查或直接拒绝」,避免把 double 静默截成 int 之类。

最后看「名单闸门」与另一条注册路径。`main()` 读 JSON 时跳过 `special_ufuncs` 名单里的名字:

[`_generate_pyx.py` L959-L967] — main:跳过 special_ufuncs 名单(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L959-L967)

```python
ufuncs = []
with open('functions.json') as data:
    functions = json.load(data)
for f, sig in functions.items():
    if (f not in special_ufuncs):       # 名单内的名字走 _special_ufuncs.cpp
        ufuncs.append(Ufunc(f, sig))
generate_ufuncs(..., ufuncs)
```

`special_ufuncs` 名单本身(节选,可见 `airy`、`agm` 等在列):

[`_generate_pyx.py` L79-L108] — special_ufuncs 名单节选(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L79-L108)

```python
special_ufuncs = [
    ...
    "_lambertw",
    ...
    "agm",
    "airy",
    "airye",
    ...
]
```

这些名字最终通过生成的 `.pyi` 桩文件从 `_special_ufuncs` 扩展模块导入:

[`_generate_pyx.py` L288-L289] — 生成的桩文件从 _special_ufuncs 导入名单(https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L288-L289)

```python
UFUNCS_EXTRA_CODE_BOTTOM = f"""\
from ._special_ufuncs import ({', '.join(special_ufuncs)})
```

> 结论:`functions.json` 是「生成式路径」的声明表,`special_ufuncs` 名单是「C++ 直注册路径」的索引(详见 u8-l3)。两条路径产出的 ufunc 在 Python 侧并无区别,但实现来源不同。本讲聚焦前者。

#### 4.3.4 代码实践

**实践目标**:用「是否含整数参数」这一条规则,预测一条签名会不会被自动派生出 float32 变体,并用 `.types` 验证。

**操作步骤**:

1. 取三条签名,先**预测**它们各自会注册哪些类型环(loop):
   - `betainc` 的 `ibeta_double: ddd->d`(无整数参数,但另有显式 float 内核)
   - `bdtr` 的 `cephes_bdtr_wrap: dpd->d`(含 `p` 整数参数)
   - `eval_chebyc` 的 `eval_chebyc[double]: dd->d`(无整数参数)
2. 在已安装 SciPy 的环境里运行(待本地验证):

   ```python
   import scipy.special as sc
   print("betainc:", sc.betainc.types)
   print("bdtr:   ", sc.bdtr.types)
   # eval_chebyc 是 ufunc,可直接看 types
   print("eval_chebyc:", sc.eval_chebyc.types)
   ```

**需要观察的现象 / 预期结果**:

- `betainc.types` 应包含 `f->f`(来自显式 `ibeta_float`)与 `d->d`(来自 `ibeta_double`);也可能因自动派生而出现更多环。
- `bdtr.types` 中由 `cephes_bdtr_wrap`(`dpd->d`)贡献的那一环**不会**带 float32(因为含整数参数 `p`,被 `iter_variants` 抑制);其 float 能力(若有)来自另一条 `bdtr_unsafe: ddd->d` 的派生。
- `eval_chebyc.types` 的实数环来自 `dd->d` 自动派生,应同时看到 `d->...`/`f->...` 之类的组合。

**预期结果**:你应能仅凭「签名里有没有 `i/l/q/p`」预测 float32 变体的有无,并与 `.types` 实际输出吻合;若环境跑不通,记为「待本地验证」,但预测结论可由 4.3.3 的源码逻辑独立推出。

#### 4.3.5 小练习与答案

**练习 1**:既然 `ibeta_double: ddd->d` 会自动派生出 `fff->f` 变体,作者为什么还要**显式**再写一条 `ibeta_float: fff->f`?

**参考答案**:自动派生的 `fff->f` 变体**复用的是同一个 `ibeta_double` 内核**——float32 输入会先被转成 double、调用 double 内核、再把结果转回 float32,失去 float32 专用的性能/精度优势。显式写 `ibeta_float` 并排在前面,能让 float32 输入命中**为 float 专门编译的 Boost 内核**(`ibeta_float`),既快又在 float32 精度下更贴合。这是「显式多内核」相对「自动派生」的核心增值点。

**练习 2**:`cast_order` 用序列 `'ilpfdgFDG'` 的下标作排序键,这把哪类类型排在最前?为什么这种排序配合「稳定排序」能保住「列在前面的内核优先」?

**参考答案**:该序列把整数(`i/l/p`)排在最前,实浮点(`f/d/g`)居中,复数(`F/D/G`)最后。Python 的 `sort` 是**稳定**的——当两个变体的排序键(类型串下标列表)相同时,它们在原列表中的相对顺序不变。而「列在前面的内核」会被先登记进 `variants`,因此同类型竞争时稳定排序不会打乱它们的先后,从而兑现 docstring 所说「functions listed earlier take precedence」。

**练习 3**:`airy` 是 special 里很常用的多输出 ufunc,但本讲为什么在 `functions.json` 里找不到它?

**参考答案**:因为 `airy` 登记在生成器的 `special_ufuncs` 名单里(见 `_generate_pyx.py` L108),它走的是**更新的纯 C++ 注册路径**——直接在 `_special_ufuncs.cpp` 里用 `xsf::numpy::ufunc` 注册,而非经由 `functions.json` → 生成 `.pyx` 的老路径。`main()` 里 `if (f not in special_ufuncs)` 这道闸门也确保即便它误入 JSON 也不会被重复生成。该 C++ 路径的细节是 u8-l3 的主题。

---

## 5. 综合实践

把本讲三个模块串起来,完成下面这个**贯穿性任务**(即本讲指定的代码实践任务):

**任务**:在 `functions.json` 中找出 `bdtr`、`betainc`、`eval_chebyc` 三个条目,用中文逐条解释「函数名 → 头文件 → 内核名 → 签名」的含义,并指出哪条用到了 C++。

**操作步骤**:

1. **定位**:打开 `scipy/special/functions.json`,分别跳到 `bdtr`(L25)、`betainc`(L67)、`eval_chebyc`(L165)。
2. **逐条解析**(参考 4.1 的四列表格写法),对每条给出:
   - 顶层 ufunc 名(对外叫什么)。
   - 中层头文件(来自哪、什么语言:`.h` = C、`.h++` = C++、`.pxd` = Cython)。
   - 内层内核名(真正的函数符号;注意融合类型特化 `[double]` 与 `_unsafe`/`_l` 后缀的含义)。
   - 签名(用 4.2 的规则翻译成「几个输入/输出、什么类型、返回值是否忽略」)。
3. **多内核分析**(用 4.3 的规则):
   - `bdtr` 有两个头文件、两套签名(`ddd->d` 与 `dpd->d`)。指出哪条会自动派生 float32 变体(提示:看是否含整数参数 `i/l/q/p`)。
   - `betainc` 在一个 C++ 头下列了 float+double 两个内核。解释为什么要显式写两个而不是靠自动派生(提示:专用 float 内核的性能收益)。
   - `eval_chebyc` 用了融合类型特化(`[double]` / `[double complex]`)和 `_l` 大阶版本。说明这三条内核各自服务什么输入。
4. **判断 C++**:仅看头文件名是否以 `++` 结尾,得出结论。

**需要观察的现象 / 预期结果**:

- `bdtr`:Cython(`_legacy.pxd`, `bdtr_unsafe: ddd->d`)+ C(`xsf_wrappers.h`, `cephes_bdtr_wrap: dpd->d`)。**不是 C++**。`ddd->d` 无整数参数 → 会自动派生 float32 变体;`dpd->d` 含 `p` → 不派生。
- `betainc`:**C++**(`boost_special_functions.h++`),内核 `ibeta_float: fff->f` 与 `ibeta_double: ddd->d`。显式双内核为 float32 提供专用实现。
- `eval_chebyc`:Cython(`orthogonal_eval.pxd`),三个内核:`eval_chebyc[double complex]: dD->D`(复数点)、`eval_chebyc[double]: dd->d`(实数点)、`eval_chebyc_l: pd->d`(大阶,用 `p` 表阶数)。**不是 C++**。

**结论**:三条中**只有 `betainc` 用到了 C++**,判据是头文件名末尾的 `++`。

**进阶验证(可选,待本地验证)**:

```python
import scipy.special as sc
# betainc 应同时有 float 与 double 类型环
print(sc.betainc.types)
# bdtr 的类型环:观察 dpd 路径不贡献 float32
print(sc.bdtr.types)
# eval_chebyc 的类型环:实/复两套
print(sc.eval_chebyc.types)
```

把 `.types` 的输出与本任务的签名解析相互印证。

---

## 6. 本讲小结

- `functions.json` 是一张**三层嵌套映射**:顶层 = ufunc 的 Python 名,中层 = 头文件(决定后端与语言),内层 = 内核函数名 + 类型签名。一条声明同时描述了「接口名、实现来源、类型契约」。
- 类型码用单字符:`f/d/g`(实浮点)、`F/D/G`(复数)、`i/l/p`(整数,`p` = 指针宽度)、`v`(仅返回值占位)。三张字典 `CY_TYPES/C_TYPES/TYPE_NAMES` 把同一个码翻译成 Cython、C、NumPy 三种拼写。
- 签名有两种形式:**单输出** `<input>-><retval>`(经返回值返回)与 **多输出** `<input>*<output>->*<retval>`(输出走指针,`*` 开头的返回值被丢弃,通常是 sf_error 状态码)。解析由 `_parse_signature` 的正则完成。
- 头文件后缀携带语言信息:`.h++` 表示 C++ 头(如 Boost.Math),`.pxd` 表示 Cython 头(可用融合类型),`.h` 表示 C 头。判一个 ufunc 是否用 C++,看中层头文件名是否以 `++` 结尾。
- **多内核分发**有两套机制:显式列出多个内核(`betainc` 的 float+double),按「列在前者优先 + cast_order 稳定排序」分发;以及 `iter_variants` 自动派生的类型变体(`i→l` 总是,`d→f/D→F` 仅当无整数参数时)。`DANGEROUS_DOWNCAST` 禁止有损转换。
- `functions.json` 并非全部 ufunc 的来源:登记在 `special_ufuncs` 名单(如 `airy`、`agm`)的名字走更新的 C++ 直注册路径(`_special_ufuncs.cpp`),`main()` 用 `if (f not in special_ufuncs)` 闸门避免重复生成。

---

## 7. 下一步学习建议

- **下一讲 u3-l2(`_generate_pyx.py`)**:本讲只读了生成器里「解释 JSON 语法」的部分。下一讲将完整拆解 `Ufunc` 类、`generate_loop`、`generate_ufuncs`,看它如何把本讲的声明**真正编译成** `_ufuncs.pyx` 与 `_ufuncs_cxx.pyx` 源码——即 `PyUFunc_FromFuncAndData` 注册调用与内层循环是如何被逐条产出的。
- **u3-l3(`meson.build`)**:看本讲的 `functions.json` 与 `_generate_pyx.py` 如何通过 `custom_target` 接入 Meson 构建管线,以及 `.pyx` 如何被 Cython generator 翻成 `.c`/`.cpp`。
- **u3-l4(C/C++ 后端版图)**:把本讲反复出现的 `xsf_wrappers.h`、`boost_special_functions.h++`、`_legacy.pxd`、`_cdflib_wrappers.pxd` 串成一张「后端调度图」,理解 `functions.json` 的中层头文件字段如何选择不同数学库。
- **延伸阅读**:想验证本讲对类型环的推断,可在安装好的 SciPy 上对任意 ufunc 调 `.types`、`.nin`、`.nout`,与本讲的签名解析相互印证(详见各模块的代码实践)。
