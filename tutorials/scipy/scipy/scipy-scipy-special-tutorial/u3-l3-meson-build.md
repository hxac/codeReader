# meson.build:扩展模块的编译目标体系

## 1. 本讲目标

本讲是「代码生成管线」单元（U3）的第三讲，承接 u3-l2（`_generate_pyx.py` 如何把 `functions.json` 翻译成 `_ufuncs.pyx` 等源文件）。上一讲我们停在「生成器产出了若干 `.pyx`/`.h`/`.pxd` 文件」，但**这些文件本身还不是能被 `import` 的模块**——它们必须经过「编译、链接」才能变成 Python 能加载的共享库（Linux 上是 `.so`）。本讲就回答这个问题。

学完本讲，你应当能够：

1. 说清楚 `scipy/special/meson.build` 一共定义了哪几个**扩展模块目标**（`py3.extension_module`），以及它们各自承载什么函数、源码来自哪里。
2. 理解 `custom_target`（跑 `_generate_pyx.py` 做代码生成）与 `generator`（把 `.pyx` 翻译成 `.c`/`.cpp`）这两种 Meson 构件如何**串联**成一条完整流水线。
3. 看懂每个目标 `dependencies` / `link_with` 里出现的 `xsf_dep`、`boost_math_dep`、`cdflib_lib`、`np_dep` 分别来自哪里、为什么有的模块需要它、有的不需要。

一句话定位：本讲是 special 模块「工程心脏」的**最后一块拼图**——把声明（u3-l1）、生成（u3-l2）落到「能编译、能链接、能加载」的真实产物上。

## 2. 前置知识

阅读本讲前，最好大致了解以下概念（不熟悉也不要紧，下面会顺带解释）：

- **编译型扩展模块（extension module）**：Python 本身是解释执行的，但 NumPy/SciPy 里那些「跑得快」的函数，其实住在用 C/C++/Cython 写、再编译成 `.so`（Linux）/`.pyd`（Windows）的**扩展模块**里。`import` 一个扩展模块时，操作系统会把它当成普通动态库加载进进程。
- **Cython**：一种「带类型注解的 Python 方言」。`.pyx` 文件先被 Cython 编译器翻译成等价的 C（或 C++）源码，再用 C/C++ 编译器编译。它是 Python 与 C/C++ 之间的「胶水」，special 的绝大多数 ufunc 内核循环都是 Cython 写的。
- **Meson**：SciPy 使用的构建系统，配置文件叫 `meson.build`。它用一种声明式语法描述「要编译哪些目标、它们的源码来自哪里、依赖什么、产出放在哪」。
- **ufunc**：NumPy 通用函数，逐元素、可广播、可批量。special 里几乎所有函数都是 ufunc（见 u2-l1）。
- **静态库 vs 共享库**：静态库（Linux 上 `.a`）是被「揉进」最终产物的一堆目标代码；扩展模块本身是一个共享库（`.so`），可以被多个进程共享加载。

如果你读过 u3-l1（`functions.json` 声明语法）和 u3-l2（`_generate_pyx.py` 生成器），本讲会非常自然；否则建议先扫一眼那两讲的结论再继续。

## 3. 本讲源码地图

本讲聚焦的文件：

| 文件 | 角色 |
| --- | --- |
| [meson.build](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build) | **本讲主角**。定义 special 的全部编译目标：1 个静态库 + 7 个扩展模块 + 代码生成 + Cython 生成器 + 安装 + 子目录递归。 |
| [_generate_pyx.py](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py) | u3-l2 精读过的离线代码生成器。本讲只关心它的 `main()` 产出哪几个文件（`_ufuncs.pyx`、`_ufuncs_cxx.pyx` 等），以及这些产出如何被 `meson.build` 接住。 |
| [scipy/meson.build](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/meson.build) 与 [根 meson.build](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/meson.build) | 提供本目录用到的若干**公共变量**：`xsf_dep`、`boost_math_dep`、`version_link_args`、`np_dep`、`cython`、`cython_gen`、`cython_gen_cpp`、`_cython_tree`、`Wno_maybe_uninitialized` 等。本讲会指出它们各自的来源行。 |

> 提示：本目录的 `meson.build` 不会凭空引用变量——所有「裸名」依赖（在本文件内没有赋值的标识符）都来自上游 `meson.build`。这是 Meson 的作用域规则：`subdir('special')` 让子目录能看见父目录里在它之前已定义的变量。

---

## 4. 核心概念与源码讲解

### 4.1 Meson 扩展模块目标体系总览

#### 4.1.1 概念说明

special 不是一个「单一的大模块」，而是由**多个互相独立的扩展模块**拼装而成的。你在 Python 里 `import scipy.special` 时，`__init__.py` 会触发这些 `.so` 一一加载（见 u1-l3/u1-l4）。

为什么拆成多个而不是一个？两个核心原因：

1. **语言隔离**：有的内核是纯 C，有的是 C++（依赖 Boost.Math），有的是手写 Cython。把 C++ 的重型编译单独隔离，能加快增量构建、缩小「改一行重编半个库」的爆炸半径——这一点 `_generate_pyx.py` 的文档串里专门做过解释（见 [第 60-69 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L60-L69)）。
2. **来源不同**：有的 ufunc 走「`functions.json` → 生成 `.pyx`」的路径，有的走「在 C++ 里直接注册」（`_special_ufuncs.cpp`），有的是手写（`_specfun.pyx`）。这些自然落在不同目标里。

在 Meson 里，一个扩展模块用 `py3.extension_module(名字, 源码列表, ...)` 声明，其中 `py3` 是 Meson 的 Python 模块对象（在 [根 meson.build:21](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/meson.build#L21) 通过 `import('python').find_installation(pure: false)` 得到）。

#### 4.1.2 核心流程

special 的 `meson.build` 自上而下组织成几块（伪代码）：

```
1. 源码列表与辅助变量定义   (_ufuncs_pxi_pxd_sources / ufuncs_sources / ufuncs_cxx_sources)
2. 静态库 cdflib_lib        (C 概率分布库)
3. 公共编译宏 ufuncs_cpp_args (-DSP_SPECFUN_ERROR)
4. 两个「直注册」C++ 扩展模块 (_special_ufuncs / _gufuncs)
5. 一个「手写 Cython」扩展模块 (_specfun)
6. 代码生成 custom_target    (cython_special，产出 _ufuncs.pyx 等 5 个文件)
7. 两个「本地」Cython generator (uf_cython_gen / uf_cython_gen_cpp)
8. 三个「吃生成产物」的扩展模块 (_ufuncs / _ufuncs_cxx / _ellip_harm_2)
9. cython_special 扩展模块    (供外部 cimport 的类型化 API)
10. npz 参考数据生成 + 纯 .py 安装 + 子目录递归
```

七个扩展模块速查表（按出现顺序）：

| # | 模块名 | 源码来源 | 关键依赖 | 用途 |
| --- | --- | --- | --- | --- |
| 1 | `_special_ufuncs` | 手写 C++（`_special_ufuncs.cpp` + 文档 + `sf_error.cc`） | xsf_dep, np_dep | 新路径：用 `xsf::numpy::ufunc` 在 C++ 里直接注册 ufunc |
| 2 | `_gufuncs` | 手写 C++（`_gufuncs.cpp` + 文档 + `sf_error.cc`） | xsf_dep, np_dep | 广义 ufunc（gufunc）注册 |
| 3 | `_specfun` | 手写 Cython（`_specfun.pyx`） | xsf_dep, np_dep | Zhang & Jin 经典特殊函数 |
| 4 | `_ufuncs` | **生成的** `_ufuncs.pyx` + C/C++ 包装 | xsf_dep, np_dep, **link cdflib_lib** | **主体**：绝大多数 ufunc 住在这里 |
| 5 | `_ufuncs_cxx` | **生成的** `_ufuncs_cxx.pyx` + Boost 包装 | **boost_math_dep**, xsf_dep, np_dep, ellint_dep | 承载 Boost.Math 重型 C++ 函数 |
| 6 | `_ellip_harm_2` | 手写 Cython（`_ellip_harm_2.pyx`） | xsf_dep, np_dep | 椭圆谐函数专项 |
| 7 | `cython_special` | 手写 Cython（`cython_special.pyx`）+ C 包装 | xsf_dep, np_dep, **link cdflib_lib** | 供外部 `cimport` 的标量类型化 API（见 U6） |

> 注意命名陷阱：`cython_special` 这个名字**同时**被用于「第 6 步的代码生成 `custom_target`」和「第 9 步的扩展模块」。它们是两个不同的 Meson 对象，只是恰好同名——下文会用「`cython_special`（custom_target）」「`cython_special`（extension_module）」并结合上下文区分。同名之所以不冲突，是因为 `custom_target` 的返回值被赋给了变量 `cython_special`，而 `extension_module('cython_special', ...)` 只是给最终 `.so` 命名，二者并不在同一个命名空间里互相覆盖。

#### 4.1.3 源码精读

先看两个「直注册」的纯 C++ 扩展模块，它们**不走**代码生成路径：

[_special_ufuncs 与 _gufuncs:L34-L52](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L34-L52)

这两个模块的源码直接是手写 `.cpp`（外加 `_special_ufuncs_docs.cpp` 提供文档串、`sf_error.cc` 提供错误处理），依赖 `xsf_dep`（xsf C++ 库）和 `np_dep`（NumPy 头文件），并通过 `cpp_args: ufuncs_cpp_args`（即 `-DSP_SPECFUN_ERROR`，见 [第 32 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L32)）开启 specfun 错误贯通。这正是 u3-l1 提到的「`special_ufuncs` 名单走另一条 C++ 直注册路径」的落点——这些函数**不会**出现在 `_ufuncs.pyx` 里，因为 `_generate_pyx.py` 的 `main()` 会跳过名单内的函数（见 [第 962-964 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L962-L964) 的 `if (f not in special_ufuncs)` 判断）。

再看承载「绝大多数 ufunc」的主体模块 `_ufuncs`：

[_ufuncs 扩展模块:L92-L108](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L92-L108)

它的源码列表由两部分拼成：`ufuncs_sources`（一组手写 C/C++ 包装，[第 14-19 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L14-L19)：`_cosine.c`、`xsf_wrappers.cpp`、`sf_error.cc`、`dd_real_wrappers.cpp`）加上 `uf_cython_gen.process(cython_special[0])`（把**生成出来的** `_ufuncs.pyx` 翻译成 `.c`）。注意它 `link_with: cdflib_lib`——这正是本讲实践任务要解释的关键点之一，4.4 节会展开。

#### 4.1.4 代码实践

**实践目标**：亲手把 7 个扩展模块盘点一遍，建立「模块名 ↔ 源码来源」的对应。

**操作步骤**：

1. 打开 [special/meson.build](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build)，用编辑器搜索 `py3.extension_module(`。
2. 对每处匹配，记录：模块名、源码列表的第一项、`dependencies:` 里出现的依赖、有没有 `link_with:`。
3. 把结果填进 4.1.2 的速查表（先别看答案）。

**需要观察的现象**：你会得到恰好 7 处匹配；其中 3 个模块（`_special_ufuncs`、`_gufuncs`、`_specfun`）的源码里**不含**任何 `cython_special[...]` 或 `uf_cython_gen`，说明它们不走代码生成；另外 4 个则依赖生成的或手写的 `.pyx` 经 generator 翻译。

**预期结果**：能复述出 `_ufuncs`、`_ufuncs_cxx`、`cython_special`、`_ellip_harm_2` 四个模块都用到了某个 `*.process(...)` 调用（即「吃」Cython 生成器产物），而前三个模块是纯手写源码。结合已安装 SciPy 也可佐证：`python -c "import scipy.special._ufuncs, scipy.special._ufuncs_cxx, scipy.special.cython_special, scipy.special._special_ufuncs, scipy.special._gufuncs, scipy.special._specfun, scipy.special._ellip_harm_2"` 能逐个成功 import，说明这 7 个 `.so` 都真实存在。

> 待本地验证（可选）：上述 import 命令在已安装 SciPy 的环境里即可运行；若某模块名不存在，说明你装的 SciPy 版本与本文 HEAD（`8e93e0478c`）不同，模块清单可能略有出入。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_specfun` 用的是 `cython_gen_cpp.process('_specfun.pyx')`，而 `_ufuncs` 用的是 `uf_cython_gen.process(...)`？这两个生成器有什么不同？

> **参考答案**：`cython_gen_cpp` 是**项目级共享**生成器（定义在 [scipy/meson.build:507-511](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/meson.build#L507-L511)），`_specfun.pyx` 是一份稳定、自包含的手写文件，用它即可；而 `uf_cython_gen` 是 special 目录内的**本地**生成器（[第 80-84 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L80-L84)），它在 `depends:` 里额外声明了对一组 `.pxi`/`.pxd` 头以及对生成产物 `cython_special[0]` 的依赖，目的是让「声明文件改动 → 重新生成 → 重新翻译 → 重新编译」这条链路自动级联。

**练习 2**：在 7 个模块里，哪两个会静态链接 `cdflib_lib`？

> **参考答案**：`_ufuncs` 和 `cython_special`（见 [第 105 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L105) 与 [第 170 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L170)）。它们都直接或间接调用 cdflib 的 C 概率分布内核（4.4 节详述）。

---

### 4.2 代码生成 custom_target:把 _generate_pyx.py 接入构建

#### 4.2.1 概念说明

`custom_target` 是 Meson 的一种构件，用来描述「跑一个外部脚本，把若干输入文件加工成若干输出文件」。它和 `extension_module` 的区别是：`extension_module` 的产出是一个会被加载的模块，而 `custom_target` 的产出是**中间源文件**——这些源文件还要被后续步骤（generator + 编译）继续加工。

在 special 里，`custom_target` 正好用来「**把 u3-l2 的代码生成器 `_generate_pyx.py` 挂进构建系统**」。没有它，`_generate_pyx.py` 只是一个孤立的脚本；有了它，每次 `functions.json`（或文档源 `_add_newdocs.py`）改动，Meson 都会自动重跑生成器，刷新 `_ufuncs.pyx` 等产物。

#### 4.2.2 核心流程

`custom_target` 的工作流：

```
输入:  _generate_pyx.py  +  functions.json  +  _add_newdocs.py
         │
         ▼  执行命令:  python _generate_pyx.py -o <输出目录>
         │   （内部: 读 functions.json → 过滤 special_ufuncs → 构造 Ufunc → generate_ufuncs）
         ▼
输出数组 (顺序固定!):
  [0] _ufuncs.pyx
  [1] _ufuncs_defs.h
  [2] _ufuncs_cxx.pyx
  [3] _ufuncs_cxx.pxd
  [4] _ufuncs_cxx_defs.h
         │
         ▼  下游用 cython_special[0] / cython_special[2] 按索引取用
```

关键细节：`custom_target` 的 `output:` 是一个**有序列表**，下游用整数索引（如 `cython_special[0]`）精确取用某一个产物。这就要求生成器写出文件的顺序与 `output:` 列表严格一致——这一致性由 `_generate_pyx.py` 的 `main()` 与 `generate_ufuncs()` 共同保证。

#### 4.2.3 源码精读

[cython_special custom_target:L62-L74](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L62-L74)

逐字段解读：

- `_generate_pyx = find_program('_generate_pyx.py')`（[第 62 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L62)）：把生成器脚本当作一个「可执行程序」注册，供 `command:` 调用。
- `input:` 列了三个文件，意味着**任何一个**改动都会触发重跑。注意 `_add_newdocs.py` 也在其中——因为生成器要从中取出每个 ufunc 的文档串（见 u3-l2 中 `Ufunc.__init__` 的 `add_newdocs.get(name)`，缺失文档串会直接抛错）。
- `command: [_generate_pyx, '-o', '@OUTDIR@']`：`@OUTDIR@` 是 Meson 占位符，展开成构建目录里本次产物的输出文件夹。
- `install: false`：生成产物是中间文件，不随包安装（最终安装的是编译后的 `.so` 和纯 `.py`）。

至于生成器内部到底按什么顺序写出这 5 个文件，对应 `_generate_pyx.py` 的 `main()`：

[main() 写出的目标文件列表:L949-L957](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L949-L957)

这里的 `dst_files` 顺序与上面 `output:` 列表**一一对应**——这正是 `cython_special[0]` 能稳定指向 `_ufuncs.pyx`、`cython_special[2]` 稳定指向 `_ufuncs_cxx.pyx` 的根本保证。随后 [第 965-967 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L965-L967) 的 `generate_ufuncs(...)` 会真正按此顺序落盘这些文件。

#### 4.2.4 代码实践

**实践目标**：追踪「生成产物如何被下游模块按索引取用」。

**操作步骤**：

1. 在 [meson.build](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build) 中搜索 `cython_special[`，记录每一处用到的索引 `[0]` 或 `[2]`。
2. 对照 4.2.2 的「输出数组」，说明每个索引对应哪个文件。
3. 找到这些索引分别喂给了哪个 `extension_module` 或 `generator`。

**需要观察的现象**：`cython_special[0]`（`_ufuncs.pyx`）出现在 `_ufuncs` 模块的源码列表（[第 95 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L95)）和两个本地生成器的 `depends`（[第 83、89 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L80-L90)）；`cython_special[2]`（`_ufuncs_cxx.pyx`）出现在 `_ufuncs_cxx` 模块的源码列表（[第 136 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L136)）。

**预期结果**：你能画出一条链：`functions.json` → `custom_target`（产出 `_ufuncs.pyx`）→ `uf_cython_gen.process` → `_ufuncs` 扩展模块。这就是「声明如何最终变成可加载模块」的完整证据链。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `input:` 里的 `_add_newdocs.py` 删掉，会发生什么问题？

> **参考答案**：从「能否运行」上 `custom_target` 仍可执行，但 Meson 将**不知道** `_add_newdocs.py` 是生成器的依赖。于是当你只改了某个 ufunc 的文档串（而不碰 `functions.json`）时，Meson 不会触发重新生成，导致 `_ufuncs.pyx` 里的文档与源码不一致、最终编译产物里的文档串过期。把 `_add_newdocs.py` 列入 `input:` 是为了让 Meson 把它纳入依赖图，实现「改文档 → 自动重生成」。

**练习 2**：`output:` 列表为什么必须是「固定的、有序的」，而不能用字典按名取用？

> **参考答案**：Meson 的 `custom_target` 设计上返回一个**有序的产物数组**，下游用整数索引引用（`target[0]`、`target[2]`）。这是 Meson 的 API 约定；生成器的写出顺序必须与之对齐，所以 `_generate_pyx.py` 才把 `dst_files` 写成一个固定顺序的元组（[第 950-954 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L950-L954)），并要求 `generate_ufuncs` 按此顺序落盘。

---

### 4.3 Cython generator:把 .pyx 翻译成 .c/.cpp

#### 4.3.1 概念说明

`generator` 是 Meson 的另一种构件，专门描述「对每一份输入文件，跑同一个翻译命令」。它与 `custom_target` 的区别在于：`custom_target` 是「一个脚本 → 一组明确列出的输出」（输入输出都枚举）；`generator` 是「**一条规则**：任意 `.pyx` → 对应的 `.c`/`.cpp`」，输入文件可变、输出文件名按模板生成。

在 special 里，generator 干的活就是把 Cython 源码（`.pyx`）翻译成 C/C++ 源码（`.c`/`.cpp`），后者再交给 C/C++ 编译器。注意：generator **只负责翻译**，编译成 `.so` 是 `extension_module` 的工作——generator 的产物会被自动并入 `extension_module` 的源码列表（通过 `.process()` 方法返回的「生成产物对象」）。

#### 4.3.2 核心流程

special 定义了两个**本地**生成器，分别产出 C 和 C++：

```
uf_cython_gen       (.pyx → @BASENAME@.c)     用于 _ufuncs.pyx / cython_special.pyx / _ellip_harm_2.pyx
uf_cython_gen_cpp   (.pyx → @BASENAME@.cpp)   用于 _ufuncs_cxx.pyx
        │
        │  arguments: cython_args / cython_cplus_args (含 --cplus、--include-dir @BUILD_ROOT@)
        │  depends:   _cython_tree + _ufuncs_pxi_pxd_sources + cython_special_pxd + cython_special[0]
        │            + cython_lapack_pxd
        ▼
  翻译出 .c / .cpp，并入对应 extension_module 的源码列表
```

- `@BASENAME@` 是 Meson 占位符，表示「输入文件去掉扩展名后的主干」，所以 `_ufuncs.pyx` → `_ufuncs.c`。
- `depends:` 列出的是「翻译时需要读到的」辅助头文件（`.pxd`/`.pxi`），以及生成产物 `cython_special[0]`（保证先生成再翻译）。
- 翻译命令本身（`cython_args`）定义在 [scipy/meson.build:474](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/meson.build#L474)：`['-3', '--fast-fail', '--output-file', '@OUTPUT@', '--include-dir', '@BUILD_ROOT@', '@INPUT@']`，其中 `-3` 指定 Python 3 语义；`cython_cplus_args` 在它前面多一个 `--cplus`（[第 499 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/meson.build#L499)），指示 Cython 产出 C++ 而非 C。

#### 4.3.3 源码精读

[本地 Cython 生成器:L80-L90](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L80-L90)

两个生成器几乎对称，差别仅在：`uf_cython_gen` 用 `cython_args` 产出 `.c`；`uf_cython_gen_cpp` 用 `cython_cplus_args`（多了 `--cplus`）产出 `.cpp`。两者的 `depends:` 都包含同一组依赖：

- `_cython_tree`：项目级 Cython 头树，起始定义在 [scipy/meson.build:472](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/meson.build#L472)；本目录在 [第 77 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L77) 又往里追加了 `__init__.pxd`（为了让 Cython 在翻译时能把 special 当作一个包做相对导入）。
- `_ufuncs_pxi_pxd_sources`（[第 1-12 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L1-L12)）：一组用 `fs.copyfile` 拷贝进构建树的 `.pxd`/`.pxi` 头，如 `sf_error.pxd`、`_complexstuff.pxd`、`_ufuncs_extra_code.pxi` 等——生成的 `_ufuncs.pyx` 顶部会 `include "_ufuncs_extra_code.pxi"`，所以翻译时这些片段必须在场。
- `cython_special_pxd`（[第 76 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L76) 拷贝的 `cython_special.pxd`）。
- `cython_special[0]`：生成的 `_ufuncs.pyx`，确保「翻译」发生在「生成」之后。
- `cython_lapack_pxd`：来自 [scipy/linalg/meson.build:39](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/linalg/meson.build#L39)（`cython_linalg[3]`），因为 linalg 子目录先于 special 被 `subdir` 处理，其变量对本目录可见。

下游如何「调用」生成器？看 `_ufuncs` 模块源码列表里的一行：

```
uf_cython_gen.process(cython_special[0]),  # _ufuncs.pyx
```

[第 95 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L95) 的注释点明了：把生成的 `_ufuncs.pyx` 喂给 `uf_cython_gen`，它会产出 `_ufuncs.c`，这个 `.c` 自动成为 `_ufuncs` 扩展模块的源码之一。`.process()` 是 generator 的方法：给定输入文件，返回对应的「生成产物对象」，可直接放进 `extension_module` 的源码列表。

#### 4.3.4 代码实践

**实践目标**：对比「本地生成器」与「项目级共享生成器」的差异，理解为何要再造一个本地版。

**操作步骤**：

1. 在 [special/meson.build](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build) 找到本地生成器 `uf_cython_gen` / `uf_cython_gen_cpp`（第 80-90 行）。
2. 在 [scipy/meson.build](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/meson.build) 找到共享生成器 `cython_gen` / `cython_gen_cpp`（第 501-511 行）。
3. 逐项对比两者的 `arguments` 和 `depends`，写下差异。
4. 回答：`_specfun.pyx` 用的是哪一个？为什么它可以用共享版，而 `_ufuncs.pyx` 必须用本地版？

**需要观察的现象**：共享版 `depends: [_cython_tree, cython_shared_module]`（[第 504、510 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/meson.build#L501-L511)）；本地版在此基础上**额外**依赖一组 `.pxi`/`.pxd` 头和生成的 `cython_special[0]`。

**预期结果**：本地版多出来的 `depends`，正是为了让 Cython 在翻译 `_ufuncs.pyx` 时能找到 `_ufuncs_extra_code.pxi` 等被 `include` 的片段，并保证翻译发生在生成之后。`_specfun.pyx` 是稳定的自包含手写文件，不依赖这些生成片段，所以用共享版 `cython_gen_cpp` 即可（[第 55 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L55)）。

> 待本地验证（可选）：若你已配置好 Meson + Ninja + 编译工具链，可以在仓库根执行 `meson setup build && ninja -C build scipy/special/_ufuncs.so`，在 `build/scipy/special/` 下观察是否真有 `_ufuncs.c`、`_ufuncs_cxx.cpp` 中间产物生成。若环境不具备，本任务退化为「源码阅读型」，不影响理解。

#### 4.3.5 小练习与答案

**练习 1**：`uf_cython_gen.process(cython_special[0])` 与 `uf_cython_gen.process('_ellip_harm_2.pyx')`（[第 147 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L147)）有何共同点？

> **参考答案**：两者都是「把一份 `.pyx` 喂给同一个本地生成器，得到一份 `.c`，并自动并入各自扩展模块的源码列表」。区别仅在输入：前者输入是**生成产物**（`cython_special[0]` 这个对象），后者输入是**手写文件**（字符串路径 `'_ellip_harm_2.pyx'`）。

**练习 2**：为什么 C++ 路径要单独用一个带 `--cplus` 的生成器，而不是和 C 路径合在一起？

> **参考答案**：因为 Cython 翻译时必须显式指定 `--cplus` 才会产出 C++（而非 C）源码。`_ufuncs_cxx.pyx` 里引用的是 Boost 等 C++ 符号，必须翻译成 `.cpp` 才能用 C++ 编译器编译、链接 Boost 头模板；而 `_ufuncs.pyx` 翻译成 `.c` 即可。两条翻译规则产物类型不同，于是定义了两个生成器各管一条。

---

### 4.4 依赖与链接:xsf_dep、boost_math_dep、cdflib_lib、np_dep

#### 4.4.1 概念说明

`extension_module` 声明里的 `dependencies:` 和 `link_with:` 决定了「编译/链接时去哪里找头文件和符号」。special 用到的几个公共依赖，各自代表一套数学后端或基础设施：

- **`xsf_dep`**：SciPy 自研的现代 C++ 特殊函数库 **xsf**（extended special functions），以子项目（subproject）形式提供，来自 [根 meson.build:210-211](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/meson.build#L210-L211) 的 `xsf = subproject('xsf'); xsf_dep = xsf.get_variable('xsf_dep')`。几乎所有模块都依赖它。
- **`boost_math_dep`**：Boost.Math 头文件库。优先用系统 Boost，否则回落到 vendored 子项目（[根 meson.build:227-238](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/meson.build#L227-L238)）。**只有 `_ufuncs_cxx` 依赖它**。
- **`cdflib_lib`**：本目录 [第 26-30 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L26-L30) 用 `static_library` 就地编译的 **C 概率分布库**（单文件 `cdflib.c`）。
- **`np_dep`**：NumPy 的头文件依赖，来自 [scipy/meson.build:81](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/meson.build#L81)，提供 `numpy/` 头与 ufunc C-API（`PyUFunc_*`）。
- 此外还有 `ellint_dep`（[第 110-125 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L110-L125)：Carlson 椭圆积分头文件集合，`declare_dependency(sources: ...)`）和 `version_link_args`（[根 meson.build:142](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/meson.build#L142)：控制导出符号可见性的链接参数）。

`dependencies` 与 `link_with` 的区别：`dependencies` 是「声明式」的依赖对象（可同时携带 include 目录、编译参数、链接库）；`link_with` 专门指「和这个静态库/共享库**链接**在一起」。所以 `cdflib_lib` 作为静态库，用 `link_with:` 挂接。

#### 4.4.2 核心流程

「函数 → 后端 → 模块 → 依赖」的落点逻辑：

```
functions.json 里某条声明
   │
   ├─ 头文件是 .pxd（如 _cdflib_wrappers.pxd，内部 cdef extern from "cdflib.h"）
   │      └─ C 内核符号住在 cdflib.c → 编进 _ufuncs.pyx → _ufuncs 模块 ──link_with──► cdflib_lib
   │
   ├─ 头文件名以 ++ 结尾（C++ Boost 内核，如 boost_special_functions.h++）
   │      └─ 函数指针导出到 _ufuncs_cxx.pyx → _ufuncs_cxx 模块 ──dependencies──► boost_math_dep
   │
   ├─ 在 special_ufuncs 名单内
   │      └─ 走 _special_ufuncs.cpp 直注册 ──dependencies──► xsf_dep (xsf::numpy::ufunc)
   │
   └─ 凡是 ufunc ──dependencies──► np_dep (NumPy C-API / PyUFunc_*)
```

这就回答了实践任务里的两个「为什么」：

- **为什么 `_ufuncs` 要 `link_with: cdflib_lib`？** 因为部分概率分布函数的「对自由度参数求逆」实现（如 `fdtridfd`、`ncfdtridfd`、`ncfdtridfn`、`stdtridf`）目前仍走经典 cdflib 算法。它们在 `functions.json` 里指向 `_cdflib_wrappers.pxd`（如 [第 292-295 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L292-L295) 的 `fdtridfd`），而这个 `.pxd` 里写的是 `cdef extern from "cdflib.h" nogil`（[_cdflib_wrappers.pxd:5](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_cdflib_wrappers.pxd#L5)）。`cdflib.h` 声明、`cdflib.c` 定义的这些 C 函数，被编译进了 `cdflib_lib` 静态库；生成的 `_ufuncs.pyx` 内层循环会调用它们，链接时就必须把 `cdflib_lib` 揉进来，否则符号未定义。
- **为什么 `_ufuncs_cxx` 要 `boost_math_dep`？** 因为凡是用 Boost.Math 实现的函数（如 `betainc`、以及已迁移到 Boost 的 `fdtr`/`ncfdtr`/`stdtr` 等），其 `functions.json` 头文件以 `++` 结尾（如 `boost_special_functions.h++`，见 [第 390-395 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L390-L395) 的 `ncfdtr`），按 u3-l2 的规则被隔离到 `_ufuncs_cxx`，而它们的真实实现就是 Boost.Math 的头文件模板，编译时必须有 Boost 头，所以 `boost_math_dep` 不可少。

而 `_ufuncs_cxx` **不**链接 `cdflib_lib`、`_ufuncs` **不**依赖 `boost_math_dep`——这种「C 函数进 `_ufuncs`、C++ 函数进 `_ufuncs_cxx`」的分离，正是 [`_generate_pyx.py` 文档串](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L60-L69) 里说的「避免在同一个共享库里同时链接 C++ 与其它语言带来的构建难题」。

> 一个易踩的坑：不要以为「概率分布函数都用 cdflib」。实际上大量 CDF/PPF（如 `fdtr`、`fdtri`、`ncfdtr`、`ncfdtri`、`stdtr`）已经迁到 Boost（`boost_special_functions.h++`），只有少数「对自由度求逆」的变体仍留在 cdflib。判据永远看 `functions.json` 里的头文件字段，而不是函数名。

#### 4.4.3 源码精读

`cdflib_lib` 的就地定义：

[cdflib 静态库:L26-L30](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L26-L30)

它把单文件 `cdflib.c` 编成一个名为 `cdflib` 的静态库，`include_directories` 指向 `../_lib` 与 `../_build_utils/src`（cdflib.h 的搜索路径），`gnu_symbol_visibility: 'hidden'` 表示默认不导出符号（只在链接时被 `_ufuncs`/`cython_special` 内部消费）。

`_ufuncs_cxx` 的依赖与 C++ 编译参数：

[_ufuncs_cxx 扩展模块:L134-L144](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L134-L144)

注意三点：`dependencies` 里**首个就是 `boost_math_dep`**；`cpp_args: ufuncs_cxx_cpp_args`（[第 127-132 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L127-L132)）带上了 `-DBOOST_MATH_STANDALONE=1`（启用 Boost.Math 的「无 Boost 主库」头文件模式）、`-DCYTHON_EXTERN_C=extern "C"`（让 Cython 生成的 C++ 代码正确处理 C 链接）、`-DSP_SPECFUN_ERROR`；源码里还通过 `ellint_dep` 引入了 Carlson 椭圆积分头（`ellint_carlson_wrap.cxx` 会用到）。

`ellint_dep` 用 `declare_dependency(sources: ellint_files)`（[第 125 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L125)）把一组**纯头文件**当成依赖（[第 110-123 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L110-L123) 列出的 `.hh`），这是一种「头文件库」的常见写法——这些 `.hh` 不单独编译，而是被 `ellint_carlson_wrap.cxx` 直接 `#include` 进去展开。

#### 4.4.4 代码实践

**实践目标**：把本讲的「依赖来源」与「cdflib 落点」事实独立验证一遍。

**操作步骤**：

1. 用 `git grep` 或编辑器搜索，确认下面四个依赖各自定义在哪：
   - `xsf_dep` → 根 `meson.build` 第 210-211 行（`xsf.get_variable('xsf_dep')`）。
   - `boost_math_dep` → 根 `meson.build` 第 227-238 行（先试系统 Boost，回落 vendored 子项目）。
   - `cdflib_lib` → special `meson.build` 第 26-30 行（就地 `static_library`）。
   - `np_dep` → `scipy/meson.build` 第 81 行。
2. 在 special `meson.build` 里统计：每个扩展模块的 `dependencies` 出现了哪些、有没有 `link_with`。
3. 打开 [functions.json](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json) 搜索 `_cdflib_wrappers.pxd`，确认走 cdflib 的函数清单（`fdtridfd`、`ncfdtridfd`、`ncfdtridfn`、`stdtridf` 等）。
4. 回答：哪个模块**既**有 `boost_math_dep` **又**有 `ellint_dep`？哪个模块**既**有 `link_with: cdflib_lib` **又**出现在 cython 类型化 API 里？

**需要观察的现象**：只有 `_ufuncs_cxx` 同时挂着 `boost_math_dep` 和 `ellint_dep`；`_ufuncs` 与 `cython_special` 都 `link_with: cdflib_lib`，而 `cython_special` 正是 U6 要讲的「供外部 `cimport` 的标量 API」。

**预期结果**：你能复述「对自由度求逆的分布函数 → `_cdflib_wrappers.pxd` → cdflib → 进 `_ufuncs`/`cython_special`」「Boost C++ 函数 → boost_math_dep → 进 `_ufuncs_cxx`」这两条独立的依赖链。

#### 4.4.5 小练习与答案

**练习 1**：`ellint_dep` 用 `declare_dependency(sources: ellint_files)` 把头文件当依赖，这种做法与 `link_with: cdflib_lib` 有何本质不同？

> **参考答案**：`ellint_dep` 携带的是**源文件**（一组 `.hh` 头），这些头会被并入依赖它的目标的源码树、由引用者（`ellint_carlson_wrap.cxx`）直接 `#include` 展开编译，不存在「单独编译的库」；而 `cdflib_lib` 是先把 `cdflib.c` 编译成**静态库**目标，再通过 `link_with` 把其已编译的目标代码链接进扩展模块。前者是「头文件库」（header-only），后者是「真静态库」。

**练习 2**：`gnu_symbol_visibility: 'hidden'` 对 `cdflib_lib` 意味着什么？

> **参考答案**：它让 cdflib 的符号默认在最终 `.so` 里**不对外可见**（不会被进程内其它扩展模块或动态链接器看到），只在编译链接 `_ufuncs`/`cython_special` 时内部解析。这避免了符号污染和跨模块符号冲突，是编写内部库的推荐做法。

---

## 5. 综合实践

把本讲三块（扩展模块目标、代码生成 `custom_target`、Cython `generator`）与依赖知识串起来，完成下面的「源码来源对照表」任务（即本讲的 `practice_task`）。

### 实践目标

在 [special/meson.build](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build) 中分别找出 `_ufuncs`、`_ufuncs_cxx`、`cython_special` 三个扩展模块目标的**源码列表**与**依赖**，解释为什么 `_ufuncs` 需要链接 `cdflib_lib` 而 `_ufuncs_cxx` 需要 `boost_math_dep`，并画出三者的「源码来源对照表」。

### 操作步骤

1. 定位三个目标：`_ufuncs`（[第 92-108 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L92-L108)）、`_ufuncs_cxx`（[第 134-144 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L134-L144)）、`cython_special`（[第 157-173 行](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L157-L173)）。
2. 对每个目标，分别抄录：源码列表（区分手写源、生成器产物）、`dependencies`、`link_with`、`cpp_args`。
3. 解释两条依赖链（参考 4.4.2 的流程图与 [functions.json](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json) 中的头文件字段）。
4. 把结果整理成下表（先自己填，再对照参考答案）。

### 参考对照表

| 维度 | `_ufuncs` | `_ufuncs_cxx` | `cython_special` |
| --- | --- | --- | --- |
| Cython 主源 | 生成的 `_ufuncs.pyx`（`cython_special[0]`）→ `.c` | 生成的 `_ufuncs_cxx.pyx`（`cython_special[2]`）→ `.cpp` | 手写 `cython_special.pyx` → `.c` |
| 手写 C/C++ 源 | `_cosine.c`、`xsf_wrappers.cpp`、`sf_error.cc`、`dd_real_wrappers.cpp` | `ellint_carlson_wrap.cxx`、`sf_error.cc` | `_cosine.c`、`xsf_wrappers.cpp`、`sf_error.cc`、`dd_real_wrappers.cpp` |
| `dependencies` | xsf_dep, np_dep | **boost_math_dep**, xsf_dep, np_dep, ellint_dep | xsf_dep, np_dep |
| `link_with` | **cdflib_lib** | （无） | **cdflib_lib** |
| 特殊 `cpp_args` | `-DSP_SPECFUN_ERROR` | `-DBOOST_MATH_STANDALONE=1`、`-DCYTHON_EXTERN_C=extern "C"`、`-DSP_SPECFUN_ERROR` | `-DSP_SPECFUN_ERROR` |
| 承载内容 | 绝大多数 C 内核 ufunc（含 cdflib 对自由度求逆的分布函数） | Boost.Math C++ 函数 + Carlson 椭圆积分 | 供外部 `cimport` 的标量类型化 API |

**关于两个「为什么」的解释**：

- `_ufuncs` 需要 `link_with: cdflib_lib`：因为部分分布函数的「对自由度参数求逆」实现（`fdtridfd`、`ncfdtridfd`、`ncfdtridfn`、`stdtridf`）仍用经典 cdflib 算法，在 `functions.json` 中指向 `_cdflib_wrappers.pxd`，而后者 `cdef extern from "cdflib.h"`。这些 C 符号的实现住在 `cdflib.c`（编进 `cdflib_lib`），内核调用被编进 `_ufuncs.pyx`；链接时必须把 cdflib 静态库揉进来才能解析符号。同理 `cython_special` 也要链接它，因为类型化 API 同样会调用这些 C 内核。
- `_ufuncs_cxx` 需要 `boost_math_dep`：因为用 Boost.Math 实现的函数（如 `betainc`、以及 `ncfdtr`/`fdtr`/`stdtr` 等已迁移的 CDF/PPF），其 `functions.json` 头文件以 `++` 结尾，按 u3-l2 的分离规则被导出到 `_ufuncs_cxx`；这些函数的真实实现是 Boost.Math 的头文件模板，编译时必须能找到 Boost 头，故依赖不可少。

### 需要观察的现象 / 预期结果

你应该能画出这样一张「数据流」图：

```
functions.json ──custom_target(_generate_pyx)──► _ufuncs.pyx ──uf_cython_gen──► _ufuncs.c ─┐
                                                                                            ├─► _ufuncs.so  (+link cdflib_lib)
                                                                                            │
                              _ufuncs_cxx.pyx ──uf_cython_gen_cpp──► _ufuncs_cxx.cpp ────────┤
                                                                                            └─► _ufuncs_cxx.so (+boost_math_dep)
```

并理解：**声明（json）→ 生成（custom_target）→ 翻译（generator）→ 编译链接（extension_module + 依赖）** 这条完整管线，正是 special 模块「工程心脏」的全貌。

---

## 6. 本讲小结

- special 由 **7 个扩展模块 + 1 个静态库** 组成，并非单一模块；不同模块承载不同语言/不同来源的函数（C 内核 / C++ Boost 内核 / 手写 Cython / C++ 直注册）。
- `custom_target('cython_special', ...)` 把 u3-l2 的 `_generate_pyx.py` 接入构建，产出 5 个文件（`_ufuncs.pyx` 等），其**有序输出数组**让下游用 `cython_special[0]`/`[2]` 精确取用；`main()` 的 `dst_files` 顺序与之严格对齐。
- 两个**本地** Cython `generator`（`uf_cython_gen` → `.c`、`uf_cython_gen_cpp` → `.cpp`）负责把 `.pyx` 翻译成 C/C++，其 `depends` 保证了「先生成、后翻译、改声明自动级联」。
- C 内核（含 `_cdflib_wrappers.pxd` 背后的 cdflib 概率分布）走 `_ufuncs` 并 `link_with: cdflib_lib`；C++ Boost 内核走 `_ufuncs_cxx` 并 `dependencies: boost_math_dep`——这一分离既隔离了编译成本，也规避了 C/C++ 混合链接难题。
- `xsf_dep`（自研 C++ 库）与 `np_dep`（NumPy C-API）是几乎所有模块的公共依赖；`ellint_dep` 是头文件式的 Carlson 椭圆积分库。
- 本讲把 u3-l1（声明）、u3-l2（生成）与「能编译能加载」的现实产物彻底打通，是理解 special 工程实现的收官。

## 7. 下一步学习建议

- **横向对照**：用同样的方法读 [scipy/linalg/meson.build](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/linalg/meson.build) 或其它子模块，体会「声明 + 生成 + 编译」这套模式在 SciPy 其它地方的复用与差异（注意 `cython_lapack_pxd` 正是从 linalg 来的）。
- **纵向深入后端**：U8 将深入 C/C++ 后端，建议接着读 u8-l1（`xsf_wrappers`）、u8-l2（`boost_special_functions.h`）、u8-l3（`_special_ufuncs.cpp` 直注册路径）、u8-l4（Carlson 椭圆积分与 cdflib），把本讲提到的依赖对应到具体内核实现。
- **动手验证（可选）**：配置 Meson + Ninja + 编译工具链后，执行 `meson setup build && ninja -C build scipy/special/_ufuncs.so`，在 `build/scipy/special/` 观察 `_ufuncs.pyx`/`_ufuncs.c`/`_ufuncs.so` 的真实生成路径，把本讲的「数据流图」变成肉眼可见的文件。
- **回到代码生成**：若你对 `cython_special[0]` 里到底生成了什么仍好奇，可重读 u3-l2 的 `Ufunc.generate` 与 `generate_ufuncs`，结合本讲的产物落点，完整复盘「一条 `functions.json` 声明如何最终变成一个可调用的 ufunc」。
