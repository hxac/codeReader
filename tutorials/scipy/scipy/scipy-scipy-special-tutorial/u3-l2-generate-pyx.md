# `_generate_pyx.py`:从 JSON 生成 Cython ufunc 代码

## 1. 本讲目标

学完本讲,你应该能够:

1. 说清楚 `_generate_pyx.py` 这个「离线代码生成器」是如何把 `functions.json` 这张声明表,变成 `_ufuncs.pyx` / `_ufuncs_cxx.pyx` 等 Cython 源文件的。
2. 复述生成主流程:`main()` 读 JSON → 过滤 `special_ufuncs` → 构造 `Ufunc` 列表 → `generate_ufuncs()` 输出五个文件。
3. 解释 `Func` / `Ufunc` 类如何用正则解析签名(`d->d`、`d*dd->*i`),`iter_variants()` 如何自动派生 `f`/`F` 变体,`cast_order()` 如何稳定排序。
4. 看懂 `generate_loop()` 生成的「内层循环」C 代码,理解 `PyUFunc_FromFuncAndData` 注册时 `loops`/`types`/`data` 三张表的对应关系。
5. 解释为什么要把 C++(Boost)内核单独生成到 `_ufuncs_cxx`,再以「函数指针」形式被 `_ufuncs` 引用。

本讲承接 [u3-l1](u3-l1-functions-json.md)(已经讲清楚了 `functions.json` 的声明语法与类型码),把视角从「声明长什么样」推进到「声明怎么变成可编译的 ufunc 注册代码」。

## 2. 前置知识

阅读本讲前,建议你已经掌握:

- **NumPy ufunc 的基本构成**。一个 ufunc 在 C 层面由「若干个 loop 函数 + 一张类型签名表」组成,通过 `PyUFunc_FromFuncAndData(funcs, data, types, ntypes, nin, nout, ...)` 注册。`funcs[i]` 是处理第 `i` 种类型组合的「内层循环」函数指针,`types[i]` 描述该 loop 的输入输出类型码序列。详见 [u2-l1](u2-l1-ufunc-fundamentals.md)。
- **`functions.json` 的声明语法**。每个 ufunc 名映射到「头文件 → {内核函数名: 类型签名}」,类型码 `f/d/g`(实浮点)、`F/D/G`(复数)、`i/l/p`(整数),签名分单输出 `input->retval` 与多输出 `input*output->*retval` 两式。详见 [u3-l1](u3-l1-functions-json.md)。
- **Meson 的 `custom_target` 概念**。`_generate_pyx.py` 是在构建期被 `custom_target` 调用的「代码生成器」,产出的 `.pyx` 再被 `generator` 翻成 `.c`/`.cpp`。详见 [u1-l3](u1-l3-build-and-test.md)。
- **一点正则表达式基础**。本讲的签名解析依赖 `re.match`,涉及字符类 `[]`、量词 `*`、捕获组 `()`。

一个直觉性的比喻:`functions.json` 像是一份「菜单」,`_generate_pyx.py` 像是「中央厨房」,它读菜单、按规则把每道菜(每个 ufunc)「炒」成一份份 Cython 代码(`_ufuncs.pyx`),最后由 Cython+Meson 把这些代码「装盘端上桌」(编译成 `.so` 扩展模块)。

## 3. 本讲源码地图

本讲只精读两个文件,外加一处构建引用:

| 文件 | 角色 |
| --- | --- |
| [_generate_pyx.py](_generate_pyx.py) | **本讲主角**,离线代码生成器。读 `functions.json`,输出 5 个文件。 |
| [functions.json](functions.json) | 声明表(数据),生成器的输入。u3-l1 已详细讲解。 |
| [meson.build](meson.build) | 构建脚本,其中 `custom_target('cython_special', ...)` 把生成器接入构建流水线。 |

生成器内部的代码组织(对应本讲三个最小模块):

- **主流程**:`main()`(L949)、`generate_ufuncs()`(L827)。
- **签名解析**:`Func` 类(L592)、`Ufunc(Func)` 类(L657)、`_parse_signature()`(L607)、`_get_signatures_and_loops()`(L692)、`iter_variants()`(L546)、`cast_order()`(L382)。
- **内层循环生成**:`generate_loop()`(L412)、`DANGEROUS_DOWNCAST`(L388)、`NAN_VALUE`(L399)。

> 提示:`_ufuncs.pyx`、`_ufuncs_cxx.pyx`、`_ufuncs_defs.h` 等 5 个产物**不在源码仓库里**,它们由构建系统在编译时实时生成。所以你看不到它们,只能通过阅读 `_generate_pyx.py` 推断其内容——这正是本讲的核心练习。

## 4. 核心概念与源码讲解

### 4.1 代码生成器主流程:从 JSON 到五个产物文件

#### 4.1.1 概念说明

`_generate_pyx.py` 不是一个被 `import` 的普通模块,而是一个**离线代码生成器**(code generator):它在构建期以 `python _generate_pyx.py -o <outdir>` 的方式被运行,读入 `functions.json`,吐出 Cython/C 源文件,然后这些源文件再进入正常的 Cython 编译流程。

这种「声明式数据 → 代码生成」的设计,是为了避免手工维护 200 多个 ufunc 各自重复的注册样板代码。生成器把「类型分发、变体派生、内层循环样板、函数指针桥接」这些机械工作集中自动化,人只需要在 `functions.json` 里写一行声明。

文件顶部的文档字符串把生成目标说得很清楚:

[文件路径:1-72](_generate_pyx.py#L1-L72) —— 生成 `_ufuncs` 与 `_ufuncs_cxx` 两个模块,「同时生成 `PyUFunc_FromFuncAndData` 调用与所需的 ufunc 内层循环」。

#### 4.1.2 核心流程

整个生成器的主干是 `main()` 函数,流程只有四步:

```text
读 functions.json
   │
   ▼
逐条遍历:名字不在 special_ufuncs 名单里的,才构造 Ufunc 对象
   │
   ▼
把所有 Ufunc 收集进列表,交给 generate_ufuncs()
   │
   ▼
generate_ufuncs() 写出 5 个文件
```

源码如下:

[_generate_pyx.py:949-967](_generate_pyx.py#L949-L967) —— `main()`:打开 `functions.json`,对每条 `f, sig`,只要 `f not in special_ufuncs` 就 `ufuncs.append(Ufunc(f, sig))`,最后调用 `generate_ufuncs()`。

这里的关键过滤条件是 `special_ufuncs`:

[_generate_pyx.py:79-269](_generate_pyx.py#L79-L269) —— 一份硬编码名单(约 190 个名字,如 `agm`、`airy`、`erf`、`jv`、`voigt_profile` 等)。

**为什么要把这份名单排除掉?** 因为名单上的函数走的**不是**「`functions.json` → 生成 `.pyx`」这条老路,而是 [u8-l3](u8-l3-special-ufuncs-registration.md) 会讲的「新路径」——直接在 C++ 文件 `_special_ufuncs.cpp` 里用 `xsf::numpy::ufunc` 注册。也就是说,`functions.json` 里登记的内容,在构建期由 `_generate_pyx.py` 处理;而 `special_ufuncs` 名单里的函数,其 ufunc 在 `_special_ufuncs` 扩展里**已经直接注册好**,生成器只需在底部把它们的成品 `from ._special_ufuncs import (...)` 转发进来即可(见 L288-289 的 `UFUNCS_EXTRA_CODE_BOTTOM`)。这就是「双轨」生成:`functions.json` 驱动生成式 vs `_special_ufuncs` 直注册式。

`main()` 最后调用 `generate_ufuncs()`,它负责真正写出文件:

[_generate_pyx.py:827-931](_generate_pyx.py#L827-L931) —— `generate_ufuncs(fn_prefix, cxx_fn_prefix, ufuncs)`:对每个 `Ufunc` 先用 `get_prototypes()` 生成函数声明与类型检查片段(区分 `.pxd` 与 `.h++`),再调 `ufunc.generate(all_loops)` 生成 ufunc 注册代码,最后拼装成五个文件。

最终产出的五个文件如下(由 `main()` 的 `dst_files` 声明):

[_generate_pyx.py:950-955](_generate_pyx.py#L950-L955) —— 五个目标文件:`_ufuncs.pyx`、`_ufuncs_defs.h`、`_ufuncs_cxx.pyx`、`_ufuncs_cxx.pxd`、`_ufuncs_cxx_defs.h`。

它们在写入时(见 `generate_ufuncs` 末尾 L905-931)的分工:

- `_ufuncs.pyx`:**主体**,包含所有非 C++ ufunc 的内层循环、函数声明、`PyUFunc_FromFuncAndData` 注册调用,以及 `seterr/geterr/errstate`(通过 `include "_ufuncs_extra_code.pxi"`)。
- `_ufuncs_defs.h`:函数原型的 C 头文件,用于 Cython 在 `cdef extern from` 时做编译期类型检查。
- `_ufuncs_cxx.pyx` / `_ufuncs_cxx.pxd` / `_ufuncs_cxx_defs.h`:C++ 专用的三件套,只导出 Boost 内核的**函数指针**(详见 4.4 节)。

构建侧,这五个文件由 `meson.build` 的 `custom_target` 一次性生成:

[meson.build:62-74](meson.build#L62-L74) —— `custom_target('cython_special', ...)`,声明输出为上述 5 个文件,输入依赖 `_generate_pyx.py`、`functions.json`、`_add_newdocs.py`,命令就是 `_generate_pyx.py -o @OUTDIR@`。

注意这个 `custom_target` 的名字也叫 `cython_special`(与另一个扩展模块同名,这是 u1-l3 提到过的同名混淆),并且 `install: false`——产物只在构建树里供 Cython 编译,不随包安装。

#### 4.1.3 源码精读

`generate_ufuncs` 的主体循环值得逐段看:

[_generate_pyx.py:851-887](_generate_pyx.py#L851-L887) —— 先按 `ufunc.name` 排序(保证生成结果稳定可复现);对每个 ufunc,`get_prototypes()` 返回它所有内核函数的原型,然后按头文件后缀分流:`++` 结尾走 C++ 分支(4.4 节),否则走常规 C 分支;最后 `ufunc.generate(all_loops)` 产出注册代码追加到 `toplevel`。

最终的拼接顺序也值得注意:

[_generate_pyx.py:890-911](_generate_pyx.py#L890-L911) —— 写入 `_ufuncs.pyx` 时,顺序是:公共/额外代码片段 → 函数声明 `defs` → 各 ufunc 的注册代码 `toplevel` → 底部别名片段。其中 `all_loops.values()`(所有内层循环函数体)排在最前,因为 Cython 要求 `cdef` 函数定义在使用之前。

#### 4.1.4 代码实践

**实践目标**:在不实际运行生成器的前提下,只读源码就推断出「`main()` 把哪些函数送进 `generate_ufuncs`,哪些被排除」。

**操作步骤**:

1. 打开 [_generate_pyx.py:949-967](_generate_pyx.py#L949-L967),确认主流程四步。
2. 打开 [_generate_pyx.py:79-269](_generate_pyx.py#L79-L269),在 `special_ufuncs` 名单里搜索 `airy`、`erf`、`jv`、`sici`、`hyp2f1`、`_cosine_cdf` 这几个名字。
3. 打开 [functions.json](functions.json),搜索同名条目。

**需要观察的现象**:

- `airy`、`erf`、`jv`、`_cosine_cdf` **同时出现在** `special_ufuncs` 名单里——意味着它们不会走 `_generate_pyx.py` 生成路径。
- `sici`、`hyp2f1`、`betainc` 等**不在** `special_ufuncs` 名单里——它们才是 `main()` 里 `if f not in special_ufuncs` 条件成立、真正被构造为 `Ufunc` 并生成的函数。

**预期结果**:你应当得出结论——「在 `functions.json` 里登记」与「由 `_generate_pyx.py` 生成」并不是一一对应;`special_ufuncs` 名单是一道「闸门」,只有被它拦下的函数才走生成式路径。这与 u3-l1 结尾提到的「`main()` 闸门避免重复生成」相呼应。

> 待本地验证:如果想实测,可在已克隆 SciPy 源码的环境里执行 `python scipy/special/_generate_pyx.py -o /tmp/out`(需保证 `_add_newdocs.py` 可被 `__import__`),再用 `ls /tmp/out` 查看 5 个产物是否齐全。这一步依赖构建环境,标记为待本地验证。

#### 4.1.5 小练习与答案

**练习 1**:`generate_ufuncs` 在写入 `_ufuncs.pyx` 时,为什么要先用 `ufuncs.sort(key=lambda u: u.name)` 排序?

**参考答案**:保证两次连续运行(或源码不变时)产出的 `.pyx` 内容**字节级稳定**(reproducible build)。如果顺序随字典遍历而变,生成的代码会反复变化,既影响构建缓存命中,也不利于 diff 审查。

**练习 2**:`main()` 里的目标文件 `dst_files` 一共 5 个,其中哪一个会被 Cython 用 `@BASENAME@.c` 规则翻成 C(而非 C++)?

**参考答案**:`_ufuncs.pyx`。它的内核来自纯 C 后端(Cephes/xsf 的 `.h`),所以用产生 `.c` 的 `uf_cython_gen`。`_ufuncs_cxx.pyx` 则用产生 `.cpp` 的 `uf_cython_gen_cpp`(见 [meson.build:80-90](meson.build#L80-L90))。

---

### 4.2 `Ufunc` 类与签名解析:把声明字符串变成结构化数据

#### 4.2.1 概念说明

`functions.json` 里的签名是人类写的字符串,如 `"ddiiddd->d"`、`"d*dd->*i"`、`"fff->f"`。生成器要先把这种字符串**解析**成结构化的「输入类型码 / 输出类型码 / 返回值类型码」三元组,才能据此生成代码。这正是 `Func` / `Ufunc` 类的职责。

`Func` 是基类,负责「解析签名 + 生成原型 + 处理 Cython 函数名」;`Ufunc(Func)` 在此基础上多了「文档串 + 生成 ufunc 注册代码」。两个类分工明确。

#### 4.2.2 核心流程

签名字符串有两种合法形式(对应单输出与多输出):

```text
单输出:   <input> -> <retval>             例 "d->d"
多输出:   <input> * <output> -> <*retval>  例 "d*dd->*i"
```

其中 `<input>`、`<output>` 是一串类型码字符;多输出形式的 `<retval>` 以 `*` 开头表示**返回值被丢弃**(通常是 `sf_error` 的 int 状态码,真正的结果通过输出指针返回)。

解析由正则完成,核心是两条 `re.match` 的「先尝试带 `*` 的多输出式,失败再尝试单输出式」:

```text
匹配 1(多输出): ^\s*([T]*)\s*\*\s*([T]*)\s*->\s*([*T]*)\s*$     需要一个字面 '*'
   失败 ↓
匹配 2(单输出): ^\s*([T]*)\s*->\s*([T]?)\s*$                     其中 T = fdgFDGilp
```

解析完成后,`Ufunc.generate()` 还要**派生变体**(variant)并排序,这一步决定了 ufunc 最终支持哪几种类型环(loop):

```text
对每个内核签名 inarg,outp:
   1) iter_variants(inarg, outp)  自动派生类型变体
        - 总是 i→l(64 位上 long 更常见)
        - 若无整数参数,再派生 d→f / D→F(产生 float32 版本)
   2) cast_order 稳定排序所有变体
        - 按 'ilpfdgFDG' 中的下标,数值越小越优先匹配
   3) 对每个变体 generate_loop() 生成内层循环
```

为什么要排序?因为 `PyUFunc_FromFuncAndData` 的 `types` 表是**自上而下**匹配的:NumPy 会按表中顺序,用第一个能与输入类型兼容的 loop。所以「列在前者优先」是一个硬约定。

#### 4.2.3 源码精读

签名解析在 `Func._parse_signature`:

[_generate_pyx.py:607-620](_generate_pyx.py#L607-L620) —— 两条正则。先尝试多输出式(要求中间有字面 `*`),失败则退回单输出式。`T = 'fdgFDGilp'` 是所有合法类型码。注意多输出式中 `ret.count('*') > 1` 会被判为非法签名(返回值最多一个 `*`)。

`Func.__init__` 把每个 `{header: {name: sig}}` 展平成一个五元组列表 `(func_name, inarg, outarg, ret, header)`:

[_generate_pyx.py:597-605](_generate_pyx.py#L597-L605) —— 遍历头文件与内核,逐条解析签名,收集进 `self.signatures`。

类型码到三种拼写的映射靠三张字典(`CY_TYPES` Cython 拼写、`C_TYPES` NumPy C 拼写、`TYPE_NAMES` NumPy 枚举常量),这在 u3-l1 已讲过,这里只引用其位置:

[_generate_pyx.py:339-375](_generate_pyx.py#L339-L375) —— 三张类型码字典。

变体派生在 `iter_variants`:

[_generate_pyx.py:546-589](_generate_pyx.py#L546-L589) —— `maps` 初始只有 `('i','l')`;若输入里没有 `i/l/q/p` 这类整数类型码,则**额外**追加一条 `'adD'→'bfF'` 规则来派生 `d→f`、`D→F` 的 float32 版本。注释明确说明:有整数参数的 ufunc 不派生 float32 版本,是为了规避 NumPy「整数数组 + float 标量」时 dtype 选择错误的 bug(gh-4895)。

> 注意:`iter_variants` 是个**生成器**,先 `yield` 原始 `(inputs, outputs)`,再 `yield` 派生后的。所以调用方 `list(iter_variants(...))[0]` 取到的总是原始变体。

排序用的 `cast_order`:

[_generate_pyx.py:382-383](_generate_pyx.py#L382-L383) —— `['ilpfdgFDG'.index(x) for x in c]`,把一串类型码映射成一个整数列表,用作排序键。`ilpfdgFDG` 这个串的顺序就是「类型提升优先级」:`i<l<p<f<d<g<F<D<G`,整数最小、复数最大。

排序发生在 `_get_signatures_and_loops` 的末尾:

[_generate_pyx.py:733-738](_generate_pyx.py#L733-L738) —— `variants.sort(key=lambda v: cast_order(v[2]))`。注释强调 sort 是**稳定排序**:类型码相同时,签名表里靠前的内核仍优先——这保证了 `functions.json` 里手动列出的内核优先级被尊重。

多输出的语义在 `_get_signatures_and_loops` 开头体现:

[_generate_pyx.py:715-724](_generate_pyx.py#L715-L724) —— `outp = re.sub(r'\*.*', '', ret) + outarg`:把返回值里的 `*xxx` 部分删掉,再把真正的输出参数 `outarg` 拼上,得到 ufunc 的输出类型序列。例如 `sici` 的签名 `d*dd->*i`:删掉 `*i` 得空串,拼上 `outarg=dd`,最终 `outp=dd`——即 ufunc 有 2 个 double 输出(Si 和 Ci)。

#### 4.2.4 代码实践

**实践目标**:用纸笔(或 REPL)跟踪一条真实签名的解析过程,验证你的理解。

**操作步骤**:

1. 选取 [functions.json:466-470](functions.json#L466-L470) 的 `sici` 条目,签名是 `"xsf_sici": "d*dd->*i"`。
2. 用 Python REPL 手动模拟 `_parse_signature`:

   ```python
   import re
   T = 'fdgFDGilp'
   sig = "d*dd->*i"
   m = re.match(rf"\s*([{T}]*)\s*\*\s*([{T}]*)\s*->\s*([*{T}]*)\s*$", sig)
   print(m.groups())  # 应得到 ('d', 'dd', '*i')
   ```

3. 再模拟 `outp` 计算:`re.sub(r'\*.*', '', '*i') + 'dd'`,预期得到 `'dd'`。

**需要观察的现象**:`sici` 的 inarg=`d`(1 个输入)、outp=`dd`(2 个输出),返回值 `*i` 被丢弃。这与 [u2-l1](u2-l1-ufunc-fundamentals.md) 讲的「sici 是多输出 ufunc,需用 `out=(a, b)` 接收」一致。

**预期结果**:你应当能口头复述——`sici(x)` 调用底层 `xsf_sici(double x, double *si, double *ci) -> int`,生成器把 `si`、`ci` 作为两个输出,把 `int` 返回值(错误码)丢弃。

> 待本地验证:REPL 里的正则结果可立即看到;若要确认实际生成的 `sici` ufunc 行为,可在已装 SciPy 环境里 `import scipy.special as sc; si, ci = sc.sici([1.0, 2.0])` 观察输出。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `iter_variants` 在输入包含整数类型码 `i/l/p` 时,**不**派生 float32(`d→f`)版本?

**参考答案**:因为 NumPy 在「整数参数是数组、浮点参数是标量」时会错误选择 dtype,导致 ufunc 选错 loop。注释里明确引用了 gh-4895 与 numpy#5895。所以宁可不为带整数的 ufunc 提供 float32 loop,以避免这个上游 bug。

**练习 2**:`cast_order('fd')` 与 `cast_order('df')` 哪个更小?这对生成的 ufunc 意味着什么?

**参考答案**:`'fdgFDG'.index('f')=3`,`index('d')=4`,故 `cast_order('f')=[3] < cast_order('d')=[4]`。意味着在生成的 `types` 表里,**float 版本的 loop 排在 double 版本之前**,NumPy 会优先尝试匹配 float loop;只有当输入是 double 时才回退到 double loop。这正是「float32 输入应走 float32 内核」的体现。

---

### 4.3 内层循环生成:`generate_loop` 是 ufunc 的逐元素心脏

#### 4.3.1 概念说明

一个 ufunc 在 C 层面最核心的东西是它的**内层循环函数**(inner loop):一个形如 `void loop(char **args, npy_intp *dims, npy_intp *steps, void *data)` 的函数,负责从输入缓冲区逐元素读取、调用数学内核、把结果写回输出缓冲区。NumPy 负责广播与分块,内层循环只关心「对一组连续的元素怎么算」。

`generate_loop()` 就是自动生成这个 C 函数的工厂。它的难点在于:同一个内核函数,可能被不同 ufunc 类型环(比如 `d->d` 和 `f->f`)复用,但中间需要做安全的类型转换,还要在转换可能丢失精度时(危险下转)给出 `sf_error` 告警并填 NaN。

#### 4.3.2 核心流程

`generate_loop(func_inputs, func_outputs, func_retval, ufunc_inputs, ufunc_outputs)` 接收两组类型码:一组描述**内核函数**的真实签名,一组描述**ufunc** 对外的签名(可能不同,只要 C 层转换成立即可)。生成流程:

```text
1. 校验:func 输入数 == ufunc 输入数;
        func 输出数 == ufunc 输出数,或 (有 retval 且 func 输出数+1 == ufunc 输出数)
2. 构造函数名:loop_<retval>_<fin>_<fout>_As_<uin>_<uout>
3. 声明缓冲区指针 ip0/ip1... 与 op0/op1...
4. 声明内核返回值变量 ov0(若 retval 当作第一个输出)
5. 生成主循环 for i in range(n):
     a. 若存在「危险下转」输入,先比较转换前后是否相等;不等则 sf_error 报 DOMAIN 并写 NaN
     b. 否则直接调用内核(用函数指针 func,经类型转换取输入)
     c. 把 ov* 写回输出缓冲区(输出端同样做危险下转检查)
     d. 各指针按 steps 前进
6. 循环末尾 sf_error.check_fpe(func_name) 检查硬件浮点异常
```

其中「危险下转」(`DANGEROUS_DOWNCAST`)是关键安全机制:`double→int`、`complex→double` 等会丢失信息甚至产生 NaN 的转换,生成器会插入运行时比较,一旦发现 `(<目标>)(<源>) != <源>`(即转换不可逆),就报告错误并写 NaN,而不是静默给出错误结果。

#### 4.3.3 源码精读

`generate_loop` 的函数签名与文档:

[_generate_pyx.py:412-450](_generate_pyx.py#L412-L450) —— 文档明确:被生成的 loop「可以传给 `PyUFunc_FromFuncAndData`」。若 `len(ufunc_outputs) == len(func_outputs)+1`,则返回值被当作**第一个输出参数**;否则返回值被忽略。

输入输出数量校验:

[_generate_pyx.py:451-456](_generate_pyx.py#L451-L456) —— 两条 `raise ValueError`,在生成期就拦截签名不匹配,避免生成出有 bug 的代码。

主循环体与内核调用:

[_generate_pyx.py:491-500](_generate_pyx.py#L491-L500) —— `for i in range(n)` 循环;`funcall` 把 `data` 解析成函数指针 `(<retval(*)(argtypes) noexcept nogil>func)(...)` 并调用。注意 `noexcept nogil`:内层循环在 NumPy 调度下运行,**不持有 GIL**,所以内核必须也是 `nogil` 安全的。

危险下转的输入检查:

[_generate_pyx.py:502-520](_generate_pyx.py#L502-L520) —— 对每个输入,若 `(ufunc_inputs[j], func_inputs[j])` 落在 `DANGEROUS_DOWNCAST` 集合里,就生成一条「转换前后相等才调用,否则报错写 NaN」的分支;否则直接调用。这正是 u2-l3 讲的「domain error 默认静默返回 NaN」在代码生成层的体现。

危险下转的集合定义:

[_generate_pyx.py:388-397](_generate_pyx.py#L388-L397) —— 列出所有「会丢精度或产生 NaN」的转换对,如 `('d','i')`(double→int)、`('D','d')`(复→实)等。

输出端写回与 FPE 检查:

[_generate_pyx.py:522-541](_generate_pyx.py#L522-L541) —— 把内核返回值 `ov*` 写回输出缓冲区(同样做危险下转检查);最后 `sf_error.check_fpe(func_name)` 把循环内累积的硬件浮点异常转换成 `sf_error` 信号——这是 [u7-l2](u7-l2-extra-code-pxi.md) 会展开的 FPE→sf_error 桥接点。

这些 loop 函数体最终被收集到 `all_loops` 字典(在 `_get_signatures_and_loops` 里 `all_loops[loop_name] = loop`),并在写 `_ufuncs.pyx` 时排在文件最前(因为 Cython 要求先定义后使用,见 [4.1.3](#413-源码精读) 引用的 L890)。

每个 ufunc 的注册代码由 `Ufunc.generate()` 产出,它把若干个 `(func_name, loop_name, inputs, outputs)` 变体装配成 `PyUFunc_FromFuncAndData` 调用:

[_generate_pyx.py:758-786](_generate_pyx.py#L758-L786) —— 声明 `ufunc_<name>_loops`、`_ptr`、`_data`、`_types`、`_doc` 五个 C 级数组,逐个填充,最后一句 `np.PyUFunc_FromFuncAndData(...)` 完成 ufunc 注册。其中 `int(len(types)/(inarg_num + outarg_num))` 就是 **ntypes**(变体数 = 总类型码数 / 每变体的输入输出数)。

`_ptr` 数组的成对填充(`2*j` 放函数指针、`2*j+1` 放函数名字符串)值得细看:

[_generate_pyx.py:775-781](_generate_pyx.py#L775-L781) —— `data` 数组的每一项是一个「函数指针 + 函数名」的对(pair),内层循环通过 `(<void**>data)[0]` 取函数指针、`(<void**>data)[1]` 取名字(用于 `sf_error` 报错时指明是哪个函数)——这与 `generate_loop` 开头 L463-464 的取法严格对应。

#### 4.3.4 代码实践

**实践目标**:读 `generate_loop` 源码,推断一个最简单签名 `d->d` 会生成什么样的内层循环。

**操作步骤**:

1. 假设有一个内核 `double myfun(double x)`,对应 ufunc 签名 `d->d`。
2. 对照 [_generate_pyx.py:458-460](_generate_pyx.py#L458-L460) 的命名规则,写出 loop 函数名。
3. 对照 [_generate_pyx.py:491-541](_generate_pyx.py#L491-L541) 的循环体结构,推断生成内容。

**需要观察的现象**:`func_inputs=ufunc_inputs=d`,无危险下转(`('d','d')` 不在 `DANGEROUS_DOWNCAST` 里),所以输入端不生成检查分支;`func_outputs=""`、`ufunc_outputs="d"`,且 `len("")+1==len("d")`,故 retval `d` 被当作第一个输出(`rv = "ov0 = "`)。

**预期结果**(示例代码,这是**推断出的**生成产物,非仓库内既有文件):

```cython
cdef void loop_d__d_As_d_d(char **args, np.npy_intp *dims, np.npy_intp *steps, void *data) noexcept nogil:
    cdef np.npy_intp i, n = dims[0]
    cdef void *func = (<void**>data)[0]
    cdef char *func_name = <char*>(<void**>data)[1]
    cdef char *ip0 = args[0]
    cdef char *op0 = args[1]
    cdef double ov0
    for i in range(n):
        ov0 = (<double(*)(double) noexcept nogil>func)(<double>(<double*>ip0)[0])
        (<double*>op0)[0] = <double>ov0
        ip0 += steps[0]
        op0 += steps[1]
    sf_error.check_fpe(func_name)
```

把这个推断与 `generate_loop` 的拼接逻辑逐一对照,你能确认每一行都来自源码里某段 `body +=`。

> 待本地验证:可在能跑生成器的环境里实际生成 `_ufuncs.pyx`,在文件中 `grep "loop_d__d_As_d_d"` 找到真实产物与本推断比对。

#### 4.3.5 小练习与答案

**练习 1**:`generate_loop` 里 `rv = "ov0 = "` 这条赋值在什么条件下成立?它的作用是什么?

**参考答案**:当 `len(func_outputs)+1 == len(ufunc_outputs)` 时成立(即内核返回值被当作 ufunc 的第一个输出)。作用是把内核的**返回值**赋给局部变量 `ov0`,后续再把 `ov0` 写进输出缓冲区。典型场景是单输出函数 `double f(double)`——返回值就是输出。

**练习 2**:为什么内层循环末尾要调用 `sf_error.check_fpe(func_name)`?

**参考答案**:数学内核可能产生硬件浮点异常(如除零、溢出),但这些异常默认不会中断执行。`check_fpe` 在每个元素循环结束后,把累积的浮点异常状态转换成 `sf_error` 信号(如 `OVERFLOW`/`DOMAIN`),从而走 special 统一的错误处理机制(默认静默返回 NaN/inf,可由 `seterr` 配置)。这是 C 层浮点异常到 Python 告警的桥梁,详见 [u7](u7-l1-sf-error-c-layer.md)。

---

### 4.4 C 与 C++ 的分离生成:为什么要有 `_ufuncs_cxx`

#### 4.4.1 概念说明

`scipy.special` 的内核来自多个后端:纯 C 的 Cephes/xsf(`.h`)、C++ 的 Boost.Math(`.h++`)。Cython 把一个 `.pyx` 翻成 `.c` 或 `.cpp`,但**一个共享库难以同时混合 C++ 内核与其它语言**的链接(文档里提到旧版 distutils 无法处理「同一 .so 里既链接 C++ 又链接 Fortran」)。

生成器的解决方案是**双模块分离**:

- `_ufuncs.pyx`:承载所有**非 C++** 内核的 ufunc,翻成 `.c`。
- `_ufuncs_cxx.pyx`:只承载 **C++(Boost)** 内核,**不注册 ufunc**,只**导出函数指针**。
- `_ufuncs.pyx` 通过 Cython 的 `cimport` 机制,从 `_ufuncs_cxx` 拿到这些函数指针,再在自己的 `PyUFunc_FromFuncAndData` 里使用——这样 Boost 内核的 ufunc 注册逻辑仍在 `_ufuncs` 里完成,但内核代码物理上住在另一个 `.so`。

#### 4.4.2 核心流程

在 `generate_ufuncs` 的主循环里,对每个内核原型按头文件后缀分流:

```text
get_prototypes() 返回 (c_name, c_proto, cy_proto, header)
   │
   ├── header 以 '++' 结尾(C++ 后端):
   │     1. 去掉 '++'
   │     2. 把声明放进 cxx_defs(给 _ufuncs_cxx.pyx)
   │     3. 导出一个 void* 函数指针 _export_<var_name>
   │     4. 在 cxx_pxd_defs 里声明该指针(供 _ufuncs cimport)
   │     5. function_name_overrides[name] = "scipy.special._ufuncs_cxx._export_<var>"
   │        ——让 _ufuncs.pyx 引用这个指针而非直接内核名
   │
   └── 否则(纯 C / .pxd 后端):
         1. 把声明放进 defs(给 _ufuncs.pyx)
         2. 把原型放进 defs_h(给 _ufuncs_defs.h)
```

`function_name_overrides` 是关键:它是一张「内核真名 → 指针别名」的映射,后续 `cython_func_name()` 会优先用它。

#### 4.4.3 源码精读

分流逻辑在 `generate_ufuncs`:

[_generate_pyx.py:855-883](_generate_pyx.py#L855-L883) —— `if header.endswith('++')`:走 C++ 分支——用 `get_declaration` 生成声明(写到 `cxx_defs` / `cxx_defs_h`),再 `cxx_defs.append(f"cdef void *_export_{var_name} = <void*>{func_name}")` 导出函数指针,并在 `cxx_pxd_defs` 里声明同名指针。最后 `function_name_overrides[c_name] = "scipy.special._ufuncs_cxx._export_" + var_name`,使得 `_ufuncs` 侧引用的是这个跨模块指针。`else` 分支是常规 C 情形。

`cython_func_name` 对 override 的处理:

[_generate_pyx.py:638-645](_generate_pyx.py#L638-L645) —— 若 `override=True` 且名字在 `function_name_overrides` 里,就把 `c_name` 换成映射值,并把前缀 `prefix` 置空(因为映射值已经是完整路径 `scipy.special._ufuncs_cxx._export_xxx`,不需要再加 `_func_` 前缀)。这一步让 `Ufunc.generate` 里 `ufunc_<name>_ptr[2*j] = <void*>...` 自动指向 C++ 模块导出的指针。

`_ufuncs_cxx` 模块的 pxd 模板预先写好了与 `_ufuncs` 共享的 `sf_error` 依赖:

[_generate_pyx.py:844-848](_generate_pyx.py#L844-L848) —— `cxx_pxd_defs` 初始两条:`from . cimport sf_error` 与一个 `_set_action` 声明,保证 C++ 模块也能访问错误处理基础设施。

文件顶部的文档串解释了这种设计的初衷:

[_generate_pyx.py:60-69](_generate_pyx.py#L60-L69) —— 「`_ufuncs_cxx` 只导出供构造某些 ufunc 时使用的函数指针……这主要是为了避免构建问题——distutils 无法在同一个共享库里同时链接 C++ 与 Fortran」。

> 历史注记:这段文档提到的 distutils 限制是早期 SciPy 的痛点。如今虽已迁移到 Meson,但「C++ 内核单独成模块 + 函数指针桥接」的架构被保留下来,因为它同时也隔离了 Boost 的编译成本(Boost 头文件非常重,集中在一个模块便于增量编译)。

#### 4.4.4 代码实践

**实践目标**:在 `functions.json` 里找一个走 C++ 路径、一个走 C 路径的函数,对比它们在生成器里的处理差异。

**操作步骤**:

1. 在 [functions.json](functions.json) 中找到 `betainc` 条目:

   ```json
   "betainc": {
       "boost_special_functions.h++": {
           "ibeta_float": "fff->f",
           "ibeta_double": "ddd->d"
       }
   }
   ```

   (见 [functions.json:67-72](functions.json#L67-L72))头文件是 `boost_special_functions.h++`,以 `++` 结尾。
2. 再找一个纯 C 路径的,例如 `_cosine_cdf`(头文件 `_cosine.h`,见 [functions.json:2-6](functions.json#L2-L6))。
3. 对照 [_generate_pyx.py:855-883](_generate_pyx.py#L855-L883) 的两个分支,分别说明二者声明会被写进哪个文件、`function_name_overrides` 是否被设置。

**需要观察的现象**:

- `betainc` 的两个内核 `ibeta_float`/`ibeta_double` 会生成到 `_ufuncs_cxx.pyx`,并导出 `_export_ibeta_float`、`_export_ibeta_double` 两个指针;`function_name_overrides` 把它们映射到 `scipy.special._ufuncs_cxx._export_ibeta_float` 等。最终 `_ufuncs.pyx` 里 `betainc` 的 ufunc 注册用的就是这些指针。
- `_cosine_cdf` 的内核 `cosine_cdf` 走 `else` 分支,声明进 `_ufuncs.pyx` / `_ufuncs_defs.h`,无 override。

**预期结果**:你应当能解释——「`functions.json` 里某 ufunc 用不用 C++,只看头文件字段是否以 `++` 结尾;一旦用了 C++,它的内核就物理隔离到 `_ufuncs_cxx`,主模块通过函数指针引用」。这也回答了本讲开头的学习目标:为什么要把 Boost(纯 C++)单独成 `_ufuncs_cxx`。

> 待本地验证:可在生成出的 `_ufuncs_cxx.pxd` 里 `grep "_export_ibeta"` 确认指针声明,在 `_ufuncs.pyx` 里 `grep "betainc_ptr"` 确认它引用的是该指针。

#### 4.4.5 小练习与答案

**练习 1**:`function_name_overrides` 这张表如果不设置(即 C++ 内核不走 override),会发生什么?

**参考答案**:`Ufunc.generate` 里 `cython_func_name(func, specialized=True)` 会返回 `_func_<内核名>`(默认前缀),但 `_ufuncs.pyx` 里根本没有这个 C++ 内核的声明(它被放到了 `_ufuncs_cxx.pyx`),链接时会因符号缺失而编译失败。override 的作用就是让 `_ufuncs.pyx` 引用一个**跨模块导入的函数指针**(`scipy.special._ufuncs_cxx._export_xxx`),而不是直接内核符号。

**练习 2**:`_ufuncs_cxx.pyx` 为什么不直接 `PyUFunc_FromFuncAndData` 注册 ufunc,而只导出指针?

**参考答案**:因为 ufunc 的 Python 名(`betainc` 等)需要在 `_ufuncs` 命名空间里统一注册,`__init__.py` 是 `from ._ufuncs import *`。如果把注册分散到 `_ufuncs_cxx`,命名空间拼装会更复杂。只导出指针、注册集中到 `_ufuncs`,既隔离了 C++ 编译,又保持了单一的 ufunc 出口。

---

## 5. 综合实践

把本讲三个最小模块串起来,完成一个「手工模拟生成器」的端到端任务。

**任务**:假设要在 `functions.json` 风格下新增一个假想函数 `myfun`,它是「`double myfun(double x)`,实现在纯 C 头文件 `myfun.h`」。请完成以下推导:

1. **写声明**。按 `functions.json` 语法,写出 `myfun` 的条目(参考 [_generate_pyx.py:14-20](_generate_pyx.py#L14-L20) 的语法说明,以及 `_cosine_cdf` 的写法)。

   ```json
   "myfun": {
       "myfun.h": {
           "myfun": "d->d"
       }
   }
   ```

2. **过主流程**。说明它在 `main()` 里因不在 `special_ufuncs` 名单,会被构造为 `Ufunc('myfun', {...})` 并送入 `generate_ufuncs()`(对照 [4.1.2](#412-核心流程))。

3. **解析与派生变体**。`_parse_signature("d->d")` 得到 `inarg='d', outarg='', ret='d'`(对照 [4.2.3](#423-源码精读))。`iter_variants('d','d')` 因输入无整数,派生出 `('f','f')` 变体,故最终有 `('d','d')` 与 `('f','f')` 两个变体(对照 [4.2.3](#423-源码精读) 的 `iter_variants`)。

4. **排序**。`cast_order('f')=[3] < cast_order('d')=[4]`,故排序后 float 变体在前,double 变体在后。

5. **推断生成的注册调用**。基于 [4.3.3](#433-源码精读) 的 `Ufunc.generate`,写出 `myfun` 的 `PyUFunc_FromFuncAndData` 注册代码(示例代码,非仓库既有产物):

   ```cython
   cdef np.PyUFuncGenericFunction ufunc_myfun_loops[2]
   cdef void *ufunc_myfun_ptr[4]
   cdef void *ufunc_myfun_data[2]
   cdef char ufunc_myfun_types[4]
   cdef char *ufunc_myfun_doc = ( "..." )
   ufunc_myfun_loops[0] = <np.PyUFuncGenericFunction>loop_d__d_As_f_f
   ufunc_myfun_loops[1] = <np.PyUFuncGenericFunction>loop_d__d_As_d_d
   ufunc_myfun_types[0] = <char>NPY_FLOAT
   ufunc_myfun_types[1] = <char>NPY_FLOAT
   ufunc_myfun_types[2] = <char>NPY_DOUBLE
   ufunc_myfun_types[3] = <char>NPY_DOUBLE
   ufunc_myfun_ptr[0] = <void*>_func_myfun
   ufunc_myfun_ptr[1] = <void*>(<char*>"myfun")
   ufunc_myfun_ptr[2] = <void*>_func_myfun
   ufunc_myfun_ptr[3] = <void*>(<char*>"myfun")
   ufunc_myfun_data[0] = &ufunc_myfun_ptr[0]
   ufunc_myfun_data[1] = &ufunc_myfun_ptr[2]
   myfun = np.PyUFunc_FromFuncAndData(ufunc_myfun_loops, ufunc_myfun_data, ufunc_myfun_types, 2, 1, 1, 0, 'myfun', ufunc_myfun_doc, 0)
   ```

   关键推导点:
   - `len(loops)==2`(两个变体),故 `_loops[2]`、`_ptr[4]`、`_data[2]`。
   - `types` 共 `2*(1+1)=4` 项,前两项是 float 变体的 `NPY_FLOAT NPY_FLOAT`,后两项是 double 变体的 `NPY_DOUBLE NPY_DOUBLE`。
   - `PyUFunc_FromFuncAndData` 的第 4 个参数 ntypes = `len(types)/(inarg_num+outarg_num) = 4/2 = 2`;第 5、6 参数是 nin=1、nout=1。
   - 由于头文件 `myfun.h` 不以 `++` 结尾,不走 override,`funcs` 用默认 `_func_myfun` 前缀。

6. **验证**。在能运行生成器的环境中,把这条声明临时加进 `functions.json`(并给 `myfun` 准备一个 docstring 以满足 `Ufunc.__init__` 的强制要求 `if self.doc is None: raise ValueError`,见 [_generate_pyx.py:687-689](_generate_pyx.py#L687-L689)),运行生成器后 `grep` 产物,与上面推断逐行比对。

> 完成本任务后,你就完整走过了一遍「声明 → 解析 → 派生 → 排序 → 生成 loop → 注册」的全链路,这正是 `_generate_pyx.py` 的全部精髓。第 6 步涉及修改源码与运行生成器,属可选验证,待本地验证。

## 6. 本讲小结

- `_generate_pyx.py` 是**离线代码生成器**,构建期被 `meson.build` 的 `custom_target` 调用,读 `functions.json` 产出 `_ufuncs.pyx` 等 5 个 Cython/C 文件。
- 主流程四步:读 JSON → 用 `special_ufuncs` 名单过滤 → 构造 `Ufunc` 列表 → `generate_ufuncs()` 写文件;名单上的函数走 `_special_ufuncs` 直注册的另一条路。
- `Func._parse_signature` 用两条正则区分单输出(`d->d`)与多输出(`d*dd->*i`)签名;`iter_variants` 自动派生 `i→l` 和(无整数时)`d→f`/`D→F` 变体;`cast_order` 稳定排序决定 loop 优先级。
- `generate_loop` 生成逐元素内层循环,核心机制是 `DANGEROUS_DOWNCAST` 检查(危险下转时报 `sf_error` 并写 NaN)和循环末尾的 `sf_error.check_fpe`(硬件浮点异常→sf_error 桥接)。
- `Ufunc.generate` 把若干变体装配成 `loops`/`types`/`data` 三张表,最终一句 `PyUFunc_FromFuncAndData` 注册 ufunc。
- C++ 内核(头文件以 `++` 结尾)被隔离到 `_ufuncs_cxx` 模块,只导出函数指针,`_ufuncs` 通过 `function_name_overrides` 以 `cimport` 方式引用——这是为了规避混合语言链接难题并隔离 Boost 编译成本。

## 7. 下一步学习建议

- **向上看构建**:本讲生成的 `_ufuncs.pyx` 如何被 Meson 编译成 `.so`,见 [u3-l3](u3-l3-meson-build.md)(扩展模块的编译目标体系)。
- **横向看后端**:`functions.json` 的「头文件」字段如何选择 C/C++ 后端(xsf/Boost/Cephes),见 [u3-l4](u3-l4-cpp-backend-landscape.md)。
- **深入错误机制**:本讲反复出现的 `sf_error`/`check_fpe`,其 C 层完整实现见 [u7-l1](u7-l1-sf-error-c-layer.md)(sf_error 的 C→Python 贯通)。
- **对比新路径**:本讲的生成式路径与 `_special_ufuncs.cpp` 直注册路径的分工与迁移趋势,见 [u8-l3](u8-l3-special-ufuncs-registration.md)。
- **亲手验证**:在有 SciPy 开发环境时,运行 `python scipy/special/_generate_pyx.py -o /tmp/out`,对照本讲推断逐一检查 5 个产物文件,这是巩固理解的最快途径。
