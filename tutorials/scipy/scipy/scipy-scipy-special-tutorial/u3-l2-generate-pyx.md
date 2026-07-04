# _generate_pyx.py：从 JSON 生成 Cython ufunc 代码

## 1. 本讲目标

本讲是 U3「代码生成管线」的第二讲，承接 u3-l1（已经搞懂 `functions.json` 的声明语法），回答一个直击工程心脏的问题：

> 既然 `functions.json` 只是一张「函数名 → 头文件 → 内核签名」的声明表，那这张表到底是**被谁、怎么**变成可以在 Python 里 `import` 的 ufunc 的？

读完本讲，你应当能够：

1. 画出 `_generate_pyx.py` 的主流程：`读 functions.json → 过滤 special_ufuncs → 构造 Ufunc 列表 → generate_ufuncs 写出五个文件`。
2. 说清楚 `Func` / `Ufunc` 类如何解析签名、如何把内核函数名转成 Cython 可调用的名字。
3. 读懂 `generate_loop` 生成的那段「逐元素内层循环」C 代码，以及 `generate` 拼出的 `PyUFunc_FromFuncAndData` 注册调用。
4. 解释为什么 `betainc`（Boost C++）和 `erf`（xsf C++）会被拆到不同的扩展模块（`_ufuncs_cxx` vs `_special_ufuncs`）里生成与注册。

## 2. 前置知识

本讲默认你已掌握 u3-l1 的内容，尤其是：

- **类型码**：`f/d/g`（单/双/长双精度浮点）、`F/D/G`（对应复数）、`i/l/p`（int/long/npy_intp）、`v`（void）。
- **签名两式**：单输出 `input->retval`，多输出 `input*output->*retval`（`*` 开头的返回值被丢弃，通常是 sf_error 状态码）。
- **头文件决定后端与语言**：`.h` 为 C、`.h++` 为 C++、`.pxd` 为 Cython 头。

还需要三个铺垫概念：

- **NumPy ufunc 的 C 构造方式**：一个 ufunc 在 C 层由 `PyUFunc_FromFuncAndData(func, data, types, ntypes, nin, nout, identity, name, doc, unused)` 创建。其中 `func` 是一组「内层循环函数指针」（每个 loop 处理一种输入/输出类型组合），`types` 是对应的类型码数组，`data` 是传给每个 loop 的附加数据（本模块用它传「真正的内核函数指针 + 函数名字符串」）。
- **Cython 的 `cdef`/`nogil`**：本生成器产出的是 `.pyx`（Cython 源），其中 `cdef` 声明 C 变量/函数，`noexcept nogil` 表示函数不抛 Python 异常且可在释放 GIL 时调用。
- **代码生成（code generation）**：用一段 Python 程序（生成器）去产出另一段程序（这里是 Cython/C）的源码文本，再交给编译器。本模块几乎所有 ufunc 都不是手写的，而是生成出来的。

一句话定位：`_generate_pyx.py` 是把「数学声明」翻译成「可编译的 ufunc 注册代码」的**编译器前端**。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
| --- | --- | --- |
| `scipy/special/_generate_pyx.py` | 代码生成器（981 行） | 本讲绝对主角，全部精读 |
| `scipy/special/functions.json` | 声明表（128 个函数） | 生成器的输入，提供具体例子 |
| `scipy/special/meson.build` | 构建脚本 | 看生成器如何被 `custom_target` 串进构建 |
| `scipy/special/_add_newdocs.py` | 文档串仓库 | 生成器从这里取每个 ufunc 的 docstring |

注意：生成器**产出**的 `_ufuncs.pyx`、`_ufuncs_cxx.pyx`、`_ufuncs_defs.h`、`_ufuncs_cxx.pxd`、`_ufuncs_cxx_defs.h` 五个文件**不在源码树里**（它们是构建产物），本讲只读「生成器」本身。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**主流程**、**Ufunc 签名解析**、**内层循环生成**。

### 4.1 模块一：代码生成器主流程

#### 4.1.1 概念说明

「主流程」回答的是：脚本被 `python _generate_pyx.py -o <outdir>` 调起后，从入口到落盘，到底走了哪几步。它涉及两个最容易被忽略、却至关重要的设计决策：

1. **`special_ufuncs` 闸门**：并非所有 special 函数都走 JSON→生成这条老路。189 个函数（`airy`、`gamma`、`erf`、`jv`、`hyp2f1` …）已经迁移到更新的「纯 C++ 直注册」路径（`_special_ufuncs.cpp`，见 u8-l3），因此**不再出现在 `functions.json` 里**，生成器对它们什么也不做，只是在生成的 `_ufuncs.pyx` 末尾用一句 `from ._special_ufuncs import (...)` 把它们重新搬进 `_ufuncs` 命名空间。生成器真正处理的，是 `functions.json` 里**剩下**的约 128 个函数（`bdtr`、`betainc`、`eval_chebyc`、`sici` …）。
2. **`_ufuncs` 与 `_ufuncs_cxx` 的分离**：Boost.Math 写在 C++ 里，而主体 `_ufuncs` 走的是 C 编译链路。为了不在一个共享库里同时混编 C++ 与 C/Fortran，生成器把 C++ 内核「外包」给 `_ufuncs_cxx` 模块——它只导出**函数指针**，再由 `_ufuncs` 通过指针间接调用。

#### 4.1.2 核心流程

主流程的伪代码：

```
main(outdir):
    chdir 到 special 目录
    functions = json.load("functions.json")          # 读声明表
    ufuncs = []
    for f, sig in functions.items():
        if f not in special_ufuncs:                  # 关键闸门
            ufuncs.append( Ufunc(f, sig) )           # 解析签名 + 取 docstring
    generate_ufuncs("_ufuncs", "_ufuncs_cxx", ufuncs) # 产出 5 个文件
```

`generate_ufuncs` 内部对每个 ufunc 做三件事：

```
for ufunc in sorted(ufuncs, key=name):
    cfuncs = ufunc.get_prototypes()        # 1. 造函数声明（编译期类型检查）
    对每个内核:
        若头文件以 "++" 结尾 → 走 cxx 分支：声明 + 导出函数指针 + 记 name override
        否则            → 走普通分支：写入 _ufuncs 的声明
    t = ufunc.generate(all_loops)          # 2. 造 ufunc 注册代码（PyUFunc_FromFuncAndData）
    toplevel += t
最后把 all_loops(内层循环) + defs(声明) + toplevel(注册) 拼成 _ufuncs.pyx，
并分别写出 _ufuncs_defs.h / _ufuncs_cxx.pyx / _ufuncs_cxx.pxd / _ufuncs_cxx_defs.h。
```

最终落盘五个文件（与 `main()` 里 `dst_files` 元组一一对应）。

#### 4.1.3 源码精读

**入口与闸门**——[_generate_pyx.py:949-967](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L949-L967)：

这段做四件事：`chdir` 到 `special`（保证后面能 `__import__('_add_newdocs')` 和读 `functions.json`）→ `json.load` 读表 → `if f not in special_ufuncs` 闸门过滤 → 调 `generate_ufuncs`。注意 `special_ufuncs` 是模块级列表常量 [_generate_pyx.py:79-269](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L79-L269)，里面 189 个名字就是「已迁移到 C++ 直注册路径、本生成器不处理」的名单。

**`_ufuncs` 末尾的反向导入**——[_generate_pyx.py:288-295](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L288-L295)：

这句 `from ._special_ufuncs import (airy, gamma, erf, …)` 会被原样追加到生成的 `_ufuncs.pyx` 末尾。它解释了一个反直觉现象：`airy` 不在 `functions.json` 里，却仍然能通过 `from ._ufuncs import *` 进到 `scipy.special`——因为它是在 `_special_ufuncs.cpp` 里用 `xsf::numpy::ufunc` 直接注册，再被「反向导入」回 `_ufuncs`。这是 u3-l1 提到的「双轨制」在生成器层面的具体落地。

**为什么拆 `_ufuncs` / `_ufuncs_cxx`**——[_generate_pyx.py:60-69](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L60-L69)（模块文档串）与 [_generate_pyx.py:853-883](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L853-L883)（cxx 分支）：

模块文档串说得很直白：`_ufuncs_cxx`「只导出函数指针」，原因是「避免构建问题——distutils 没法在同一个共享库里同时链接 C++ 和 Fortran」。

当某内核的头文件以 `++` 结尾（如 `betainc` 的 `boost_special_functions.h++`），生成器：剥掉 `++` → 把声明写进 `cxx_defs`（最终成为 `_ufuncs_cxx.pyx`）→ 用 `cdef void *_export_<var> = <void*><func>` 导出一个函数指针 → 在 `_ufuncs_cxx.pxd` 里声明同名指针 → 设一个 `function_name_overrides`，让 `_ufuncs.pyx` 里的 ufunc 调用 `scipy.special._ufuncs_cxx._export_<var>` 而非直接调 C++ 函数。这样 C 编译的 `_ufuncs` 只依赖一个 C 链接的函数指针，C++ 的复杂性被隔离在 `_ufuncs_cxx` 这一个扩展模块里。

**生成器如何被构建系统调用**——[meson.build:62-74](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L62-L74)：

`find_program('_generate_pyx.py')` 把脚本注册为可执行程序，`custom_target('cython_special', …)` 声明：输入是 `_generate_pyx.py + functions.json + _add_newdocs.py`，命令是 `[_generate_pyx, '-o', '@OUTDIR@']`，输出是那五个文件。任何一项输入变了，Meson 就会重跑生成器——这也是为什么你**永远不要手编** `_ufuncs.pyx`。

#### 4.1.4 代码实践

**实践目标**：亲手把主流程在脑子里跑一遍，并验证「闸门」与「反向导入」两个关键设计。

**操作步骤**：

1. 打开 [_generate_pyx.py 的 main()](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L949-L967)，对照上面的伪代码，逐行标注它做了什么。
2. 在 `functions.json` 里搜索 `airy`、`gamma`、`erf`、`jv`、`hyp2f1`（可用编辑器查找），确认它们**不在** `functions.json` 中；再在 `special_ufuncs` 列表 [_generate_pyx.py:79-269](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L79-L269) 里找到它们，确认它们**在**名单里。
3. 反过来，确认 `bdtr`、`betainc`、`eval_chebyc`、`sici` **在** `functions.json` 中、**不在** `special_ufuncs` 中——它们才是 `main()` 真正会生成代码的对象。
4. 在已安装 SciPy 的环境里运行（**待本地验证**）：

   ```bash
   python -c "import scipy.special._ufuncs as u; print(type(u.airy), type(u.bdtr), type(u.betainc))"
   ```

**需要观察的现象**：`bdtr`、`betainc` 来自 JSON 生成路径，`airy` 来自 `_special_ufuncs` 反向导入，但三者最终都是 `numpy.ufunc` 且都挂在同一个 `_ufuncs` 模块下。

**预期结果**：三个都打印 `<class 'numpy.ufunc'>`，体现「生成路径不同、命名空间统一」。

#### 4.1.5 小练习与答案

**练习 1**：假如有人把 `airy` 重新加回了 `functions.json`，`main()` 会发生什么？会不会重复生成？

**答案**：不会重复生成。`main()` 的 `if f not in special_ufuncs` 闸门会跳过它，`airy` 仍只走 `_special_ufuncs.cpp` 路径。这个 `if` 正是为防止「迁移到 C++ 路径的函数还残留在 JSON 里」而设的安全网。

**练习 2**：`generate_ufuncs` 开头有一句 `ufuncs.sort(key=lambda u: u.name)`（[L851](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L851)），生成的 `.pyx` 里函数定义为什么需要按名字排序？

**答案**：为了让生成产物**稳定、可 diff**。如果按字典插入顺序输出，`functions.json` 里条目重排就会引起 `_ufuncs.pyx` 巨大无意义的 diff；按名字排序后，输出与输入顺序解耦，便于 code review 和合并冲突处理。

---

### 4.2 模块二：Ufunc 签名解析

#### 4.2.1 概念说明

`Ufunc` 类是「一条 JSON 声明」在内存里的表示。它要完成三件事：

1. **解析签名**：把 `"ddd->d"`、`"D*DD->*i"` 这样的字符串拆成 `(输入, 输出, 返回值)` 三个类型码串。
2. **取文档串**：每个 ufunc 必须有 docstring（来自 `_add_newdocs.py` 的 `get(name)`），没有就报错——这是为了保证公开 API 文档齐全。
3. **生成内核声明与可调用名**：把内核 C 函数名（可能带融合类型后缀 `[double]`）转成 Cython 里能 `cimport`/调用的名字，并在编译期做类型检查。

`Ufunc` 继承自 `Func`：`Func` 负责「解析签名 + 造声明」，`Ufunc` 额外负责「取 docstring + 生成 ufunc 注册代码」。这个继承分层让「签名解析」这一通用能力可被复用。

#### 4.2.2 核心流程

签名解析靠两条正则（按优先级）：

```
多输出式： ([fdgFDGilp]*) * ([fdgFDGilp]*) -> ([*fdgFDGilp]*)
            └─ input ──┘     └─ output ─┘     └─ retval（可带一个 *）
单输出式： ([fdgFDGilp]*) -> ([fdgFDGilp]?)
            └─ input ──┘      └─ retval ─┘
```

- 多输出式命中时，retval 若含 `*`，表示「返回值要被丢弃」（典型是 sf_error 状态码 `*i`）。
- 单输出式命中时，输出为空、返回值就是唯一结果。

解析后，每个内核变成一个五元组 `(func_name, inarg, outarg, ret, header)` 存进 `self.signatures`。

#### 4.2.3 源码精读

**`Func.__init__` 与 `_parse_signature`**——[_generate_pyx.py:597-605](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L597-L605)：

`Func.__init__` 遍历「头文件 → {内核名: 签名}」两层字典，对每条签名调 `_parse_signature`，把结果 `(name, inarg, outarg, ret, header)` 追加到 `self.signatures`。

[_parse_signature：两条正则分别吃多输出式与单输出式](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L607-L620)

注意 `_parse_signature` 对 `ret.count('*') > 1` 会主动报错（一个返回值最多一个 `*`），这是对声明表的早期校验。

**`Ufunc.__init__` 强制要求 docstring**——[_generate_pyx.py:685-690](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L685-L690)：

`add_newdocs = __import__('_add_newdocs')`（[L337](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L337)）拿到文档仓库，`add_newdocs.get(name)` 取串并 `dedent`。这一行 `raise ValueError(f"No docstring for ufunc {name!r}")` 是一道质量门：**新增 ufunc 却忘了在 `_add_newdocs.py` 写文档，生成阶段就会直接失败**，而不是带着空文档发布。

**`cython_func_name`：内核名 → Cython 可调用名**——[_generate_pyx.py:638-654](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L638-L654)：

它处理三种情况：(a) `function_name_overrides` 命中（C++ 外包场景，见 4.1）则替换并去掉前缀；(b) 名字带 `[double]` 融合类型后缀时，用正则 `^(.*?)(\[.*\])$` 拆出基名与特化部分，特化时把 `[double complex]` 的空格替换成下划线；(c) 默认加 `_func_` 前缀。例如 `eval_chebyc[double]` 特化后变成 `_func_eval_chebyc[double]`。

**`get_prototypes` 与 `get_declaration`：造编译期类型检查的原型**——[_generate_pyx.py:622-636](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L622-L636) 与 [_generate_pyx.py:791-824](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L791-L824)：

`get_prototypes` 为每个内核同时算出 C 原型 `c_proto`（给 `_defs.h`）和 Cython 原型 `cy_proto`（给 `.pyx`）。关键技巧在 `get_declaration`：对 `.pxd` 来源用 `ctypedef` + 取地址做编译期签名比对；对 `.h` 来源则用 `cdef extern from` 重新声明，让 Cython 在编译时校验「JSON 声明的签名」与「头文件里的真实签名」是否一致——**签名写错，编译期就报错，而不是运行时崩**。

#### 4.2.4 代码实践

**实践目标**：用真实声明验证 `_parse_signature` 的两条分支。

**操作步骤**：

1. 打开 `functions.json` 的单输出例子 [_cosine_cdf: d->d](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L2-L5) 与多输出例子 [sici: D*DD->*i](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L466-L470)。
2. 在脑子里（或本子上）对这两条分别套 `_parse_signature` 的两条正则：
   - `cosine_cdf: "d->d"` → 命中**单输出式**，得 `inarg="d", outarg="", ret="d"`。
   - `xsf_csici: "D*DD->*i"` → 命中**多输出式**，得 `inarg="D", outarg="DD", ret="*i"`（`*i` 表示返回的 int 状态码将被丢弃，真正的输出是两个 `D` 指针）。
3. 解释 `sici` 为何是「一次输入、两个输出」的 ufunc（正弦积分 Si 与余弦积分 Ci 同时返回）。

**需要观察的现象**：同一条签名语法既能表达「函数返回值即结果」，也能表达「结果走输出指针、返回值是状态码」。

**预期结果**：你能对着任意一条 `functions.json` 声明，说出它的 `(inarg, outarg, ret)` 三元组，并判断返回值是否被丢弃（看 `ret` 是否以 `*` 开头）。本步为纯源码阅读，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`eval_chebyc` 的声明是 [functions.json:165-170](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L165-L170)，含 `eval_chebyc[double complex]: "dD->D"`、`eval_chebyc[double]: "dd->d"`、`eval_chebyc_l: "pd->d"` 三个内核。`Func.__init__` 会把它们存成什么？

**答案**：三个独立五元组，都挂在同一个 `eval_chebyc` 这个 ufunc 名下：
`("eval_chebyc[double complex]", "dD", "", "D", "orthogonal_eval.pxd")`、
`("eval_chebyc[double]", "dd", "", "d", "orthogonal_eval.pxd")`、
`("eval_chebyc_l", "pd", "", "d", "orthogonal_eval.pxd")`。
运行时 ufunc 按输入 dtype 在这三个内核里选一个——这就是 u3-l1 讲的「多内核分发」在数据结构层面的体现。

**练习 2**：为什么 `Ufunc.__init__` 找不到 docstring 要直接 `raise`，而不是给个空串继续？

**答案**：因为生成器是构建期的「质量关卡」。若放行空 docstring，会有函数带着空文档发布到公开 API；现在直接报错，能把「忘写文档」这个失误**挡在编译之前**，强制开发者补齐 `_add_newdocs.py`。

---

### 4.3 模块三：内层循环生成

#### 4.3.1 概念说明

这一模块是整个生成器的「机械加工车间」，产出两样东西：

1. **内层循环函数（loop）**：每个 `(内核签名, ufunc输入输出类型)` 组合对应一个 `loop_...` 函数。它在 C 层遍历数组元素，逐个把输入 cast 成内核要的类型、调用内核、把结果 cast 回 ufunc 输出类型、推进指针。
2. **ufunc 注册块**：把若干 loop + 类型表 + 内核函数指针拼成一句 `PyUFunc_FromFuncAndData`，正式注册成一个 ufunc。

此外，`iter_variants` 负责**自动派生类型变体**：声明里写的是 `d->d`，生成器会自动再补一个 `f->f` 的 float32 loop（前提是没有整数参数），让同一个 ufunc 同时支持单/双精度。这是「声明一条、生成多条」的核心机制。

#### 4.3.2 核心流程

**变体派生**（`iter_variants`）的规则：

- 总是把整数 `i` 提升为 `l`（64 位上 long 更通用）。
- 当输入里**没有** `i/l/q/p` 任一整数类型码时，再额外派生一个把 `d→f`、`D→F` 的 float32 双胞胎。
- 派生是「无损替换」；`DANGEROUS_DOWNCAST`（如复数→实数、浮点→整数）则被 `generate_loop` 在运行时用 `if` 守卫，转不过去就写 NaN 并报 domain error。

**变体去重与排序**（`_get_signatures_and_loops`）：以「ufunc 输入类型串」为 key 去重（先到先得），最后按 `cast_order` 稳定排序——越「窄」的类型（f=3）排在越前面（d=4），这样 NumPy 选 loop 时优先匹配窄类型、避免不必要的精度提升。

类型码在「类型阶梯」上的位置由 `cast_order` 给出：

\[
\text{cast\_order}(c) = [\, \text{"ilpfdgFDG".index}(x) \text{ for } x \text{ in } c \,]
\]

即 `i=0, l=1, p=2, f=3, d=4, g=5, F=6, D=7, G=8`。值越小越「窄」、优先级越高。

#### 4.3.3 源码精读

**`iter_variants`：派生 int→long 与 float32 双胞胎**——[_generate_pyx.py:546-589](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L546-L589)：

注意 [L573-L580](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L573-L580) 的注释和条件：含整数参数的 ufunc **不**派生 float32 版本，因为「整数数组 + 浮点标量」会让 NumPy 选错 dtype（这是对 NumPy 一个已知行为的工作绕过，见 gh-4895）。这就解释了为什么 `bdtr`（声明 `dpd->d`，含 `p`）没有 float32 loop，而 `betainc`（`fff->f`/`ddd->d`）有完整的单双精度。

**`generate_loop`：产出逐元素循环**——[_generate_pyx.py:412-543](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L412-L543)：

它产出的函数命名是 `loop_{retval}_{func_inputs}_{func_outputs}_As_{ufunc_inputs}_{ufunc_outputs}`，签名固定为 `cdef void name(char **args, np.npy_intp *dims, np.npy_intp *steps, void *data) noexcept nogil`——这正是 NumPy 对 ufunc 内层循环的标准 C 签名。要点：

- [L463-L464](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L463-L464) 从 `data` 里取出真正的内核函数指针和函数名字符串（名字传给 `sf_error` 用于报错）。
- [L478-L489](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L478-L489) 当 `len(func_outputs)+1 == len(ufunc_outputs)`，说明内核返回值要当作第一个输出（多输出 ufunc），于是声明 `ov0` 接住返回值。
- [L504-L518](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L504-L518) 对 `DANGEROUS_DOWNCAST` 的输入做运行时守卫：cast 后再 cast 回去比较，不等就写 `NAN`（见 [NAN_VALUE 表 L399-L409](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L399-L409)，整数用 `0xbad0bad0` 标记非法）并报 domain error。
- [L541](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L541) 每个元素处理完调 `sf_error.check_fpe(func_name)`，把硬件浮点异常转成 sf_error 信号——这是 u7 要深挖的 FPE 检测入口。

**`Ufunc.generate`：拼装 PyUFunc_FromFuncAndData**——[_generate_pyx.py:740-788](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L740-L788)：

它声明四个 C 级数组（`loops`/`ptr`/`data`/`types`）和一个 docstring 指针，逐个赋值，最后一句：

```cython
<name> = np.PyUFunc_FromFuncAndData(
    ufunc_<name>_loops, ufunc_<name>_data, ufunc_<name>_types,
    <ntypes>, <nin>, <nout>, 0, '<name>', ufunc_<name>_doc, 0)
```

其中 `ntypes = len(types)/(nin+nout)`（每个 loop 占 `nin+nout` 个类型码）。`ptr` 数组成对存放 `(内核函数指针, 函数名字符串)`，`data[j] = &ptr[2*j]`——这样每个 loop 运行时都能从 `data` 拿到「自己该调哪个内核、报错时该报哪个名字」。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：给一个假想函数手写 `functions.json` 风格的声明，推断并写出 `_generate_pyx.py` 会为它生成的 `PyUFunc_FromFuncAndData` 注册块。

**操作步骤**：

1. 假设你有一个 C 内核 `double myfun_c(double x)`，定义在 `myheader.h`。在 `functions.json` 风格下，声明应是：

   ```json
   "myfun": {
       "myheader.h": {
           "myfun_c": "d->d"
       }
   }
   ```

   同时要在 `_add_newdocs.py` 里给 `myfun` 配一段 docstring（否则 `Ufunc.__init__` 会报错）。

2. 套用本模块的规则推断生成产物。`iter_variants("d","d")` 因无整数参数，会派生出 float32 双胞胎，最终两个 loop：`loop_d_d__As_f_f`（输入 f→内核 d→输出 f）与 `loop_d_d__As_d_d`（纯 double）。`cast_order` 排序后 f 在前。

3. 写下你推断的生成代码，然后对照下面的参考答案。

**需要观察的现象**：一条 `d->d` 声明，最终膨胀成「2 个 loop + 4 项类型表 + 4 项指针 + 一句注册调用」。

**预期结果 / 参考答案**：`Ufunc.generate` 会为 `myfun` 产出（示意，省略 docstring 与 `loops/types` 赋值细节）：

```cython
cdef np.PyUFuncGenericFunction ufunc_myfun_loops[2]
cdef void *ufunc_myfun_ptr[4]
cdef void *ufunc_myfun_data[2]
cdef char ufunc_myfun_types[4]
# ... loops[0]=loop_d_d__As_f_f ; loops[1]=loop_d_d__As_d_d ...
# ... types = [NPY_FLOAT, NPY_FLOAT, NPY_DOUBLE, NPY_DOUBLE] ...
ufunc_myfun_ptr[0] = <void*>_func_myfun_c          # float32 loop 的内核
ufunc_myfun_ptr[1] = <void*>(<char*>"myfun")        # 报错用的名字
ufunc_myfun_ptr[2] = <void*>_func_myfun_c          # double loop 的内核
ufunc_myfun_ptr[3] = <void*>(<char*>"myfun")
ufunc_myfun_data[0] = &ufunc_myfun_ptr[0]
ufunc_myfun_data[1] = &ufunc_myfun_ptr[2]
myfun = np.PyUFunc_FromFuncAndData(
    ufunc_myfun_loops, ufunc_myfun_data, ufunc_myfun_types,
    2, 1, 1, 0, 'myfun', ufunc_myfun_doc, 0)
```

其中 `2`(ntypes) `1`(nin) `1`(nout) 来自 `int(len(types)/(inarg_num+outarg_num))` = `4/(1+1)`。

> 说明：以上为依据 [_generate_pyx.py:740-788](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L740-L788) 与 `iter_variants` 的规则 [_generate_pyx.py:546-589](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L546-L589) **手推的示例代码**，并非项目原有产物（`myfun` 是假想函数）。真实运行需把声明加进 `functions.json`、把文档加进 `_add_newdocs.py`，再用 Meson 触发 `custom_target` 重生成——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`betainc` 的声明是 [functions.json:67-72](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L67-L72)，给了 `ibeta_float: fff->f` 和 `ibeta_double: ddd->d` 两个**显式**内核。`iter_variants` 还会为它再派生 float32 loop 吗？最终有几个 loop？

**答案**：不会再派生。`ibeta_double`（`ddd->d`）虽然无整数参数、`iter_variants` 会算出一个 `fff->f` 变体，但 `_get_signatures_and_loops` 用「ufunc 输入串」去重（[L700-L701](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L700-L701)），`fff` 这个槽位已被显式的 `ibeta_float` 占据，所以派生的那份被跳过。最终 `betainc` 恰好 **2 个 loop**（float + double），与声明一一对应——这正是「显式提供单双精度双内核」的设计意图（见 u8-l2）。

**练习 2**：为什么 `generate_loop` 末尾要调 `sf_error.check_fpe(func_name)`？

**答案**：因为 ufunc 内层循环跑的是 C 数值代码，硬件浮点异常（除零、溢出等）不会自动变成 Python 信号。`check_fpe` 在每个元素求值后检查浮点状态标志，若置位则通过 `sf_error` 机制按用户配置（`seterr`/`errstate`，见 u2-l3、u7）转成 warn/raise。这一行是把「硬件 FPE」接入「special 错误处理体系」的桥梁。

---

## 5. 综合实践

把三个模块串起来，完成一次「逆向追踪 + 顺向推断」：

**任务**：选定 `functions.json` 里的 `bdtr`（声明见 [functions.json:25-31](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L25-L31)，含 `_legacy.pxd` 的 `bdtr_unsafe: ddd->d` 与 `xsf_wrappers.h` 的 `cephes_bdtr_wrap: dpd->d` 两个内核），完整描述它从声明到 ufunc 的全过程。

**要求**：

1. **主流程**：确认 `bdtr` 不在 `special_ufuncs` 名单（故会被 `main()` 处理）；指出它的两个内核分别来自 `.pxd`（Cython）和 `.h`（C）头文件，故都不会走 `_ufuncs_cxx` 的 C++ 外包分支。
2. **签名解析**：用 `_parse_signature` 写出两个内核的 `(inarg, outarg, ret)` 三元组；解释 `cephes_bdtr_wrap` 的 `dpd->d` 里那个 `p`（npy_intp）参数为何会**阻止** `iter_variants` 派生 float32 loop。
3. **内层循环**：推断 `bdtr` 最终会有哪些 loop（提示：`bdtr_unsafe` 的 `ddd->d` 会被 `iter_variants` 派生出 float32 变体 `fff->f`，而 `cephes_bdtr_wrap` 的 `dpd->d` 因含 `p` 不会派生；多个内核竞争同一输入类型串时按声明顺序 + `cast_order` 稳定排序去重）。
4. **验证**：在已安装 SciPy 的环境运行 `python -c "import scipy.special as sc; print(sc.bdtr.types)"`，把你推断的类型串集合与 `.types` 实际输出对照——**待本地验证**。

**预期收获**：你能对着任意一条 `functions.json` 声明，独立推断出「它会被生成器处理吗 → 走哪条分支 → 派生哪些 loop → 最终 ufunc 支持哪些类型」，从而具备「读声明即懂运行时行为」的能力。

## 6. 本讲小结

- `main()` 的主流程是 `读 functions.json → if f not in special_ufuncs 过滤 → 构造 Ufunc → generate_ufuncs 写五个文件`；其中 `special_ufuncs` 闸门把 189 个已迁移到 C++ 直注册路径的函数排除在外。
- `Ufunc` 继承 `Func`：`Func` 用两条正则把签名解析成 `(输入,输出,返回值)`，`Ufunc` 额外强制要求 docstring（缺文档即编译前报错）。
- `iter_variants` 自动派生 `int→long` 与（无整数参数时的）`d→f/D→F` float32 双胞胎，实现「声明一条、生成多条」；含整数参数的函数（如 `bdtr`）不派生 float32。
- `generate_loop` 产出 NumPy 标准签名的逐元素 C 循环，对 `DANGEROUS_DOWNCAST` 做运行时守卫（失败写 NaN + domain error），并在每个元素后调 `sf_error.check_fpe` 接入错误处理体系。
- `Ufunc.generate` 把 loops/类型表/函数指针拼成 `PyUFunc_FromFuncAndData` 注册调用；`_ufuncs_cxx` 通过「导出函数指针 + name override」把 Boost C++ 内核隔离在单独的 C++ 扩展模块里，避免 C/C++ 混编的构建难题。
- 整条管线由 `meson.build` 的 `custom_target('cython_special', …)` 驱动，输入一变（`functions.json`/`_add_newdocs.py`/生成器自身）即自动重生成。

## 7. 下一步学习建议

- **横向接续（构建侧）**：本讲的 `custom_target` 只是入口，建议下一讲 u3-l3 精读 `meson.build`，看那五个生成文件如何被 Cython `generator` 翻成 `.c`/`.cpp`、再编成 `_ufuncs` / `_ufuncs_cxx` 等扩展模块，以及各自链接 `cdflib_lib`、`boost_math_dep` 的来龙去脉。
- **纵向深入（错误处理）**：本讲多次出现 `sf_error.check_fpe` 与 `DANGEROUS_DOWNCAST` 报 domain error，这些都汇入 u7 的 C 层 `sf_error` 体系；想搞懂「生成出来的 ufunc 如何报错」可直接进 u7-l1。
- **后端对照**：本讲把 `_ufuncs`（JSON→生成）与 `_special_ufuncs`（C++ 直注册）并称为「双轨制」，u8-l3 会专门拆解 `xsf::numpy::ufunc` 这条更新的注册路径，建议对照阅读以理解 special 的演进趋势。
- **动手延伸**：若想真正跑通 4.3.4 的 `myfun`，需要在一个可编辑 SciPy 源码树里改 `functions.json` + `_add_newdocs.py`，再用 `pip install -e . --no-build-isolation` 触发 Meson 重新生成；这是验证你推断的最直接方式。
