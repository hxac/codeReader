# Dtype 校验代码生成

> 本讲承接 [u8-l1 __init_subclass__ 钩子与自动安装](u8-l1-codegen-init-subclass-hook.md)。u8-l1 讲清楚了「`Op.__init_subclass__` 在子类定义瞬间据 manifest 自动合成三个 codegen 契约方法」的总机制；本讲钻进其中一个契约——`_validate_dtypes`——的合成细节。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `synthesize_validate_dtypes` 如何把 manifest `signature.inputs` 的 dtype 表达式编译成一段**纯 Python 函数体**，以及为什么用 `exec` 而不是写一个通用求值器。
2. 手写出一个给定 signature（含 `|` 并集、`same_as(ref)`、`same_as` 嵌在并集里）后，codegen 应合成出的 `_validate_dtypes` 函数体。
3. 解释 `same_as` / 并集 / `dtype_combos`（R6）三种构造在合成期与运行期各自的处理时机。
4. 讲清验证器 L3 的 parity 探测：它为什么用 `inspect.signature(...).bind(...)` 绑定，以及 codegen 为什么**必须**生成显式命名参数（而不是 `**kwargs`）才能让 parity 探测无缝工作。
5. 解释 `maybe_install_validator` 与 `maybe_install_eval_roofline` 在 MRO（方法解析顺序）处理上的**关键不对称**，以及由此引出的「手写版必须绑在具体类 `__dict__`」约束。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自前置讲义）：

- **Op(L2) / Kernel(L1) 双层分离**（[u1-l1](u1-l1-spec-driven-philosophy.md)）：`_validate_dtypes` 属于 L2 主机侧的校验逻辑，与 GPU 无关。
- **三个 codegen 契约方法**（[u8-l1](u8-l1-codegen-init-subclass-hook.md)）：`_infer_output_shapes`、`_validate_dtypes`、`eval_roofline`，均由 `__init_subclass__` 钩子据 manifest 自动合成。
- **manifest signature**（[u4-l2](u4-l2-signature-shape-rules.md)）：`signature.inputs` 是「输入名 → 属性 dict」的**有序映射**，`dtype` 字段是 dtype 表达式。
- **信任模型四阶段**（[u9-l1](u9-l1-trust-model-stages.md)）：manifest 是规约真相来源；代码服从规约，不可改规约迎合代码。

补充三个本讲会用到的 Python 基础概念，怕初学者卡住：

- **`exec` 执行字符串源码**：`exec(source, globals_dict)` 把一段字符串当 Python 代码跑，跑完把其中定义的名字写进 `globals_dict`。TileOPs 用它把合成的函数体「物化」成一个真的 `function` 对象。注意这是**类定义期一次性执行**，不是运行期反复 `eval`——这与 roofline codegen「禁止任何运行期求值器」的设计取向一致。
- **`inspect.signature` / `Signature.bind`**：`inspect.signature(fn)` 反射出一个函数的形参表；`.bind(*args, **kwargs)` 模拟一次调用，检查实参能否对上形参而不真正执行函数体。验证器用它做 parity 探测。
- **MRO（Method Resolution Order）**：Python 查找属性时遍历的类继承链。`cls.__dict__` 只看本类自己定义的属性；沿 MRO 往上找则包括所有父类。本讲的不对称就出在这两者的区别上。

一个最关键的心智锚点：**manifest 的 dtype 表达式是「声明」，合成出的 `_validate_dtypes` 是这份声明的「可执行投影」**。同一份声明被两套机制消费——codegen 把它编进 Op 类，验证器用它反向探测 Op 类是否守约。本讲讲的就是这两者如何通过「显式命名参数」这一约定锁死对齐。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tileops/ops/_dtype_codegen.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py) | **本讲主角**。`synthesize_validate_dtypes` 合成函数体；`maybe_install_validator` 决定是否装到子类上。 |
| [tileops/ops/op_base.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py) | `__init_subclass__` 钩子调用 `maybe_install_validator`；`_validate_dtypes` 的 L1 stub。 |
| [scripts/validate_manifest.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py) | L3 parity 探测：`check_l3_validate_dtypes_parity`、`_combo_accepted`、`_probe_out_of_union`。 |
| [tileops/manifest/bmm.yaml](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/bmm.yaml) | 真实样例：`BmmFwdOp`（`same_as` 无 combos）与 `BmmFp8Op`（`dtype_combos` + 手写 override）。 |
| [docs/design/manifest.md](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/manifest.md) | R4（dtype 语法）、R5（`promote_int_to_float`）、R6（`dtype_combos`）规约。 |
| [docs/design/ops-design.md](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md) | Step 5：`_validate_dtypes` 的手写模板与 codegen 约定。 |

## 4. 核心概念与源码讲解

### 4.1 `synthesize_validate_dtypes`：从 signature 合成校验函数

#### 4.1.1 概念说明

`_validate_dtypes` 是 Op 在 `forward` 里调用的一段 dtype 校验代码。它的职责很窄：**逐个检查传进来的张量 dtype 是否落在 manifest 声明的允许集合里，不合规就抛 `ValueError`**。它不做形状校验（那是 `_infer_output_shapes` / `shape_rules` 的事），也不碰 GPU。

问题是：每个算子的允许 dtype 集合都不一样，难道要给 184 个算子各手写一份？这正是 codegen 的用武之地——manifest `signature.inputs` 已经**声明**了每个输入的 dtype 表达式，codegen 只要把这份声明**翻译**成一段等价的 Python 函数体，装到 Op 类上即可。`synthesize_validate_dtypes` 就是这个翻译器。

设计上有两个取向值得记住：

1. **合成期把工作做满，运行期函数体极简**。所有 `|` 切分、`same_as(ref)` 解析、`getattr(torch, ...)` 拿真实 `torch.dtype` 对象、`dtype_combos` 归一化，都在**类定义期**一次性算好，存进闭包变量。运行期函数体只做「取实际 dtype → 查集合 / 比引用」这种近乎零开销的操作。
2. **用 `exec` 物化显式命名参数的函数**，而不是写一个吃 `**kwargs` 的通用求值器。这一点是本讲第 4.3 节 parity 探测的关键，先按下不表。

#### 4.1.2 核心流程

`synthesize_validate_dtypes(op_name, sig)` 的执行流程：

```text
入参 sig = manifest 的 signature 块
 │
 ├─ 1. 取 sig["inputs"]（有序映射）；空则报错
 │
 ├─ 2. 对每个 input 预解析 dtype 表达式：
 │      _parse_tokens("float16 | same_as(a)") → ["float16", "same_as(a)"]
 │      _classify_tokens(...) → (concrete=[torch.float16], refs=["a"])
 │      存入 per_input[name] = (concrete, refs, 原始字符串)
 │
 ├─ 3. 合成期校验：每个 same_as(ref) 的 ref 必须是同级 input
 │      （把拼写错误变成类构造期错误，而非延后到运行期 fallback）
 │
 ├─ 4. _parse_dtype_combos 归一化 sig["dtype_combos"]（若有）
 │      → combo_keys: set[tuple]  或  None（表示笛卡尔积全合法）
 │
 ├─ 5. 构造闭包 closure = {per_input, input_names, combo_keys, ...}
 │      用 exec 执行拼好的源码字符串：
 │        def _validate_dtypes(self, a, b):   ← 显式命名参数镜像 inputs
 │            ...
 │      物化出真正的 function 对象
 │
 └─ 6. 设 __name__ / __qualname__，返回该函数
```

注意 `exec` 出来的函数体**引用闭包里的 `per_input` / `input_names` / `combo_keys`**——这些名字在 `exec` 时作为 globals 注入（见第 4.1.3 节的 `closure` 字典），所以函数体里能直接读到它们。这是「合成期算好、运行期只查」的实现手法。

#### 4.1.3 源码精读

先看函数签名与入参契约——它吃的是 manifest 的 `signature` 块，要求里面有非空的 `inputs` 映射，可选 `dtype_combos`：

[tileops/ops/_dtype_codegen.py:159-184](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L159-L184) — `synthesize_validate_dtypes` 的签名与 `inputs` 解析入口，空 `inputs` 直接报错。

预解析每个 input 的 dtype 表达式，把字符串编译成 `(concrete_dtypes, same_as_refs, 原始字符串)` 三元组：

[tileops/ops/_dtype_codegen.py:186-201](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L186-L201) — 逐 input 预解析：`_parse_tokens` 切 `|`，`_classify_tokens` 分出具体 dtype 与 `same_as` 引用，存进 `per_input`。

两个辅助函数很短，但定义了「什么算合法 token」：

[tileops/ops/_dtype_codegen.py:34-60](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L34-L60) — `_parse_tokens` 按 `|` 切分；`_classify_tokens` 用正则 `_SAME_AS_RE` 识别 `same_as(ref)`，其余用 `getattr(torch, tok)` 拿真实 `torch.dtype`，拿不到（非 dtype）即抛错。

合成期对 `same_as(ref)` 做一次「拼写检查」——`ref` 必须是同一份 signature 里声明过的 input：

[tileops/ops/_dtype_codegen.py:207-214](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L207-L214) — 合成期校验 `ref` 是同级 input，把 `same_as(typo)` 这类错误提前到类构造期。

接下来是本讲最核心的一段——用 `exec` 物化函数。先看**为什么是显式命名参数**（注意 `params_src = ", ".join(input_names)` 拼出的形参表），再看闭包注入：

[tileops/ops/_dtype_codegen.py:245-258](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L245-L258) — `closure` 把合成期算好的 `per_input`/`input_names`/`combo_keys` 作为 globals 注入；`def _validate_dtypes(self, {params_src}):` 的形参名严格镜像 `signature.inputs` 的键。

注释里点出了两个理由（本讲第 4.3 节会展开第二个）：**显式命名参数让 `inspect.signature` 原生报告 manifest 的输入名**；若改成 `**kwargs` 体，则每次调用都要付一次 `Signature.bind` 的开销，在 `forward()` 热路径上可测。

再看合成的函数体本身——它就是一段「取实际 dtype → 查 concrete 集合 → 否则比 same_as 引用 → 否则抛错 → 末尾查 combos」的直白逻辑：

[tileops/ops/_dtype_codegen.py:259-292](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L259-L292) — 合成的函数体：`_locals = locals()` 取命名参数，逐个 input 先查 `_concrete` 集合，再遍历 `_refs` 比 `_ref_tensor.dtype`，都不中则抛 `ValueError`；最后若 `combo_keys is not None` 再做跨张量组合校验。

最后 `exec` 执行、取出函数、改 `__name__`/`__qualname__`：

[tileops/ops/_dtype_codegen.py:293-297](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L293-L297) — `exec` 物化，从 `closure` 取出函数并命名。

> **关于「热路径」的一点诚实校正**：`docs/design/ops-design.md:30` 说 `_validate_dtypes` runs on every `forward()` call，但实际实现里有 `_active_sig` 快路径——签名没变就跳过校验。例如 [tileops/ops/gemm.py:126-128](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/gemm.py#L126-L128) 在 `sig != self._active_sig` 时才调用 `self._validate_dtypes(a, b)`。所以更准确的说法是：**它在签名变化（缓存未命中）时运行**；codegen 仍把它当热路径relevant 来优化（去掉 per-call `Signature.bind`），因为新签名首次命中时它确实会跑。

#### 4.1.4 代码实践

**实践目标**：亲手跑一次 `synthesize_validate_dtypes`，看它合成出的函数体到底长什么样。

**操作步骤**：

1. 在仓库根目录启动 `python`（无需 GPU，纯 CPU 即可）。
2. 跑下面这段脚本（**示例代码**，非项目自带）：

   ```python
   from tileops.ops._dtype_codegen import synthesize_validate_dtypes
   import torch, inspect

   sig = {
       "inputs": {
           "a": {"dtype": "float16 | bfloat16"},
           "b": {"dtype": "same_as(a)"},
       }
   }
   fn = synthesize_validate_dtypes("MyOp", sig)
   print("signature:", inspect.signature(fn))

   class FakeOp: pass
   fake = FakeOp()
   # 合法：a=fp16, b=fp16
   fn(fake, torch.empty(0, dtype=torch.float16), torch.empty(0, dtype=torch.float16))
   print("fp16/fp16 通过")
   # 非法：a=fp16, b=bf16（违反 same_as）
   try:
       fn(fake, torch.empty(0, dtype=torch.float16), torch.empty(0, dtype=torch.bfloat16))
   except ValueError as e:
       print("fp16/bf16 被拒:", e)
   ```

**需要观察的现象**：

- `inspect.signature(fn)` 打印出 `(self, a, b)`——形参名严格镜像 `inputs` 的键 `a`/`b`，正是第 4.1.3 节强调的「显式命名参数」。
- `fp16/fp16` 通过；`fp16/bf16` 抛 `ValueError`，报错信息里含原始 dtype 字符串 `"same_as(a)"`。

**预期结果**：通过；报错信息形如 `MyOp: input 'b' has dtype torch.bfloat16, expected 'same_as(a)'`。

**待本地验证**：上述报错文案的具体措辞以你本地运行输出为准（版本不同可能微调）。

#### 4.1.5 小练习与答案

**练习 1**：`synthesize_validate_dtypes` 为什么在合成期（而非运行期）调用 `getattr(torch, tok)` 把字符串变成 `torch.dtype` 对象？

**参考答案**：为了让运行期函数体只做 `_actual in _concrete` 这种集合查找——集合里的元素已经是 `torch.dtype` 对象，比较是 O(1) 且无字符串解析开销。若延后到运行期，每次 `forward` 签名变化都要重新 `getattr`，纯属浪费；更糟的是拼写错误（如 `floatt16`）会被拖到运行期才暴露，而非类构造期。

**练习 2**：合成的函数体里 `_locals = locals()` 这一行的作用是什么？为什么后面用 `_locals[_name]` 而不直接写 `_name`？

**参考答案**：`input_names` 是运行期才知道的列表（由 manifest 决定），函数体在一个 `for _name in input_names:` 循环里要按变量名取对应的命名参数值。Python 没法把字符串变量 `_name` 直接当标识符求值，所以用 `locals()` 拿到「形参名 → 实参对象」的字典，再用 `_locals[_name]` 间接取。这是 `exec` 合成动态形参函数时的标准手法。

---

### 4.2 `same_as` / 并集 / `dtype_combos` 的展开

#### 4.2.1 概念说明

manifest 的 dtype 表达式一共有四种构造，规约定义在 `manifest.md` 的 R4–R6：

| 构造 | 语法 | 语义 | 例 |
| --- | --- | --- | --- |
| 普通 token | `float16` | 该张量必须是这个具体 dtype | `x: {dtype: "float16"}` |
| `\|` 并集 | `float16 \| bfloat16` | 取并集中的任意一个 | `a: {dtype: "float16 \| bfloat16"}` |
| `same_as(ref)` | `same_as(a)` | 运行期必须与 `ref` 张量**同 dtype** | `b: {dtype: "same_as(a)"}` |
| `same_as` 在并集里 | `float32 \| same_as(input)` | 要么是列出的具体 token，要么与 ref 同 dtype | 较少见，elementwise 多输入场景 |

[docs/design/manifest.md:44](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/manifest.md#L44) — **R4**：`|` 表 alternatives；`same_as(ref)` 是「dtype-only 身份约束」，运行期必须与 `ref` 完全同 dtype，**不**在 R6 的笛卡尔积里贡献独立轴，也**不**能用于形状。

还有一种与 `_validate_dtypes` 相关但**不归它管**的构造：

[docs/design/manifest.md:46](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/manifest.md#L46) — **R5**：`promote_int_to_float(ref)` 是**输出侧专用**构造（整型 ref → `float32`，否则 `same_as(ref)`）。它只允许出现在 `signature.outputs[*].dtype`，**不得**出现在输入、`dtype_combos` 行或 `workloads` 里。所以 `_dtype_codegen`（只处理输入校验）不处理它；它由验证器的 dtype 解析器（`_resolve_tensor_dtype_options`）在算输出 dtype 选项时展开。

第四种构造是**跨张量**的：

[docs/design/manifest.md:63-75](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/manifest.md#L63-L75) — **R6**：`dtype_combos` 显式枚举**合法的跨张量 dtype 组合**。出现 = 穷举（只列出的组合合法）；缺省 = 所有笛卡尔积都合法。用于合法集是严格子集的场景（如混合精度 GEMM）。

关键区分：`|` 并集和 `same_as` 是**单张量**约束（每个输入各自声明允许的 dtype）；`dtype_combos` 是**跨张量**约束（某些组合虽然单看都合法，但搭配起来不支持）。例如单看 `a: float16|bfloat16`、`weight: float8_e4m3fn|float16` 都自洽，但也许只有 `(fp16, fp16)` 与 `(fp16, fp8)` 两种搭配真被 kernel 支持——这时就要 `dtype_combos` 来钉死。

#### 4.2.2 核心流程

四种构造在 codegen 里的处理时机不同：

```text
单张量构造（普通/并集/same_as/并集里的same_as）
  └─ 合成期：_classify_tokens 一次性切成 (concrete[], refs[])
  └─ 运行期（合成函数体内）：
        for 每个 input _name:
            _actual = tensor.dtype
            if _actual in _concrete:   continue     ← 普通 token / 并集
            for _ref in _refs:                       ← same_as（含并集里的）
                if _actual == _ref_tensor.dtype: matched; break
            if not matched: raise ValueError

跨张量构造（dtype_combos）
  └─ 合成期：_parse_dtype_combos 把每行解析成 {name: torch.dtype}
             再聚合成 combo_keys: set[tuple]（按 input_names 顺序取值）
             （行内 same_as 在此解析为同级兄弟的具体 dtype）
  └─ 运行期（合成函数体末尾）：
        if combo_keys is not None:
            _observed = tuple(各 input 的实际 dtype)
            if _observed not in combo_keys: raise ValueError
```

注意 `dtype_combos` 的**行内 `same_as` 解析**比单张量 `same_as` 更绕：单张量 `same_as(a)` 是运行期才比 `a` 的实际 dtype；而 `dtype_combos` 行里的值**必须是单个具体 dtype**（不许写并集），但允许写 `same_as(同级兄弟)`，由 `_parse_dtype_combos` 在合成期就沿兄弟链解析到具体 dtype（带环检测）。这是 R3 身份约束（同一行里 same_as-bound 张量必须与 ref 一致）的体现。

#### 4.2.3 源码精读

先看 `dtype_combos` 的解析器——它最复杂，含两遍扫描与环检测：

[tileops/ops/_dtype_codegen.py:63-156](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L63-L156) — `_parse_dtype_combos`：第一遍类型检查（值必须是字符串、不许含 `|`、键必须是已声明 input）；第二遍迭代解析行内 `same_as` 兄弟链到具体 dtype，带 `seen` 列表做环检测。

行内 `same_as` 解析的迭代逻辑（第 124-154 行）值得细看：一个 `same_as` 可能链式经过多个兄弟才到达具体 dtype（`c: same_as(b)`、`b: same_as(a)`、`a: float16`），所以用 `while True` 沿 `cur = ref` 推进，遇到具体 token 就 `norm[name] = dt; break`，遇到已见过的名字就报环。这正是规约里 R4「`same_as` 是身份约束，与声明顺序无关」的代码落地。

再看 `combo_keys` 的构造——它把每行按 `input_names` 固定顺序取值，聚合成元组集合：

[tileops/ops/_dtype_codegen.py:218-243](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L218-L243) — `combo_keys` 构造。R6 保证每行枚举所有声明 input，故 observed 元组按 `input_names` 顺序取值即可覆盖全集；键集合缺失/多余即报错。

合成函数体末尾的 combos 校验——一个集合成员判断：

[tileops/ops/_dtype_codegen.py:282-291](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L282-L291) — 运行期 combos 校验：观测到 `(dtype_a, dtype_b, ...)` 元组不在 `combo_keys` 集合里就抛错。

现在看两个**真实 manifest 样例**对照。第一个是「纯 `same_as`、无 combos」的 `BmmFwdOp`——所有笛卡尔积合法，所以不写 `dtype_combos`：

[tileops/manifest/bmm.yaml:10-14](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/bmm.yaml#L10-L14) — `BmmFwdOp`：`a: float16|bfloat16`，`b` 与输出 `d` 均 `same_as(a)`。无 `dtype_combos` → 合法组合就是 `{a ∈ {fp16,bf16}} × {b == a}`。

对这条 signature，codegen 合成的 `per_input` 是：

```python
per_input = {
    "a": ([torch.float16, torch.bfloat16], [],            "float16 | bfloat16"),
    "b": ([],                          ["a"],            "same_as(a)"),
}
# combo_keys = None  → 笛卡尔积全合法，函数体末尾跳过 combos 校验
```

第二个样例是「有 `dtype_combos` 且**手写 override**」的 `BmmFp8Op`——它故意手写 `_validate_dtypes` 来在 forward 期拒绝 `fp8_e5m2`（manifest 的 `dtype` 字段写的是 `float8_e4m3fn`，但 `dtype_combos` 用来钉死笛卡尔积，防止验证器把 `(e5m2, *)` 当合法）：

[tileops/manifest/bmm.yaml:56-73](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/bmm.yaml#L56-L73) — `BmmFp8Op`：`dtype_combos` 显式列出两行（输出 bf16 / fp16 各一行），把合法跨张量组合钉死。

[tileops/ops/bmm.py:203-216](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/bmm.py#L203-L216) — `BmmFp8Op._validate_dtypes` 是**手写 override**（在 `cls.__dict__` 里），所以 codegen 不会给它合成——这正是 `maybe_install_validator` 第一个早退条件（见 4.3.3）。手写版与合成版承担同样的 parity 契约（见下一节）。

> **一个易混点**：`BmmFp8Op` 同时有手写 `_validate_dtypes` **和** `dtype_combos`。这两者不冲突——`dtype_combos` 是 manifest 声明（给验证器做 parity 探测用），手写 `_validate_dtypes` 是运行期执行体。验证器会用手写版去探测，要求它接受所有列出的 combo、拒绝至少一个未列出的组合。manifest 与代码各司其职，正是信任模型的体现。

#### 4.2.4 代码实践

**实践目标**：感受 `dtype_combos` 从「缺省笛卡尔积」切换到「穷举子集」时，合成函数体末尾那段校验如何启用。

**操作步骤**：

1. 用第 4.1.4 节的脚本基础上，给 `sig` 加一个 `dtype_combos`，对比两次合成的函数行为（**示例代码**）：

   ```python
   from tileops.ops._dtype_codegen import synthesize_validate_dtypes
   import torch, inspect

   # 版本 A：无 dtype_combos —— 笛卡尔积全合法
   sigA = {"inputs": {"a": {"dtype": "float16 | bfloat16"},
                      "b": {"dtype": "same_as(a)"}}}
   # 版本 B：dtype_combos 钉死成只允许 (fp16, fp16)
   sigB = {**sigA,
           "dtype_combos": [{"a": "float16", "b": "float16"}]}

   fnB = synthesize_validate_dtypes("MyOp", sigB)
   print("sigB signature:", inspect.signature(fnB))

   class FakeOp: pass
   fake = FakeOp()
   # (fp16, fp16) 在 combo 里 → 通过
   fnB(fake, torch.empty(0, dtype=torch.float16), torch.empty(0, dtype=torch.float16))
   print("(fp16,fp16) 通过")
   # (bf16, bf16) 单看合法、same_as 也满足，但不在 combo 里 → 被拒
   try:
       fnB(fake, torch.empty(0, dtype=torch.bfloat16), torch.empty(0, dtype=torch.bfloat16))
   except ValueError as e:
       print("(bf16,bf16) 被 combos 拒:", e)
   ```

**需要观察的现象**：版本 B 里 `(bf16, bf16)` 虽然通过了单张量 `same_as` 校验（`b.dtype == a.dtype`），却**被末尾的 combos 校验拒掉**，报错信息提到 `dtype_combos`。

**预期结果**：通过；报错信息形如 `MyOp: dtype combination (a=torch.bfloat16, b=torch.bfloat16) is not listed in signature.dtype_combos`。

**待本地验证**：报错文案以本地运行输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_parse_dtype_combos` 拒绝 combo 值里出现 `|`（即拒绝行内写并集）？

**参考答案**：`dtype_combos` 的语义是「枚举**具体**的合法跨张量组合」。每行每个张量必须是**单个**具体 dtype，这样验证器才能用一行去探测一次、判断接受/拒绝。若允许行内并集，一行就代表多个组合，parity 探测的「逐组合接受 / 拒绝」语义就乱了；且 R3 身份约束（same_as-bound 张量必须与 ref 一致）也要求每行是确定的具体 dtype 才能校验。所以并集只在单张量 `dtype` 字段允许，`dtype_combos` 行内必须具体。

**练习 2**：`combo_keys` 为什么用 `tuple(row[n] for n in input_names)` 而不是直接把 `row`（dict）放进集合？

**参考答案**：Python 的 `dict` 不可哈希，不能放进 `set`；而运行期观测到的组合也要能做成可哈希的键去查集合。按 `input_names`（一个固定的有序 list）取值，既解决了可哈希问题（元组可哈希），又保证了「声明的顺序」与「观测的顺序」一致——两者都用同一个 `input_names` 投影，集合成员判断才对得上。

---

### 4.3 验证器 L3 parity 探测

#### 4.3.1 概念说明

codegen 把 manifest 声明编译进 Op 类；但这只是**单向**翻译——万一翻译错了呢？万一有人手写的 `_validate_dtypes` 与 manifest 声明不一致呢？验证器的 L3 dtype parity 检查就是**反向**的守门人：它拿 manifest 声明当标尺，去**探测** Op 类上的 `_validate_dtypes` 是否守约。

parity（对等）探测的思路很直接：

- **接受侧**：manifest 声明合法的每个组合，`_validate_dtypes` **必须接受**。
- **拒绝侧**：从未声明的集合里取一个「越界」组合，`_validate_dtypes` **必须拒绝**。

两侧都过，才算代码与规约对齐。这条检查对**codegen 合成版**和**手写 override**一视同仁——它不关心函数体从哪来，只关心行为是否与 manifest 一致。

探测的核心手法是 `inspect.signature(fn).bind(mock_self, **tensors)`：**先绑定形参**（只验证签名匹配，不执行函数体），绑定通过后才真正调用函数体观察接受/拒绝。这一步正是「显式命名参数」约定的回报——下一节展开。

#### 4.3.2 核心流程

parity 探测的总流程（`check_l3_validate_dtypes_parity`）：

```text
入参：op_name, entry(manifest 条目), cls(Op 类)
 │
 ├─ 1. 若 cls 未 override _validate_dtypes → 发警告（manifest-derived
 │      method not yet generated），跳过；建议降级 spec-only。不静默通过。
 │
 ├─ 2. 解析每个张量的 dtype 选项 _resolve_tensor_dtype_options(sig)
 │      （same_as 沿引用解析到具体 dtype，promote_int_to_float 按 R5 展开）
 │
 ├─ 3. 构造 mock_self = cls.__new__(cls)，注入 self.dtype、static_dims
 │      （因为合成/手写体常写 `if x.dtype != self.dtype: raise`）
 │
 ├─ 4. 接受侧：
 │      若有 dtype_combos：逐行探测，每行必须被接受
 │      若无 dtype_combos：枚举笛卡尔积，每个组合必须被接受
 │      （每次探测：inspect.signature(fn).bind(mock_self, **tensors)
 │                 → 绑定 OK 后 fn(mock_self, **tensors) 观察是否抛错）
 │
 ├─ 5. 拒绝侧 _probe_out_of_union：
 │      从一个已知接受的 baseline 出发，逐个把非 same_as 输入换成
 │      越界 sentinel，每个候选必须被拒（same_as-bound 输入随 ref 传播）
 │      self.dtype 钉在 baseline 主 dtype，只让输入张量越界
 │
 └─ 6. 汇总 errors（硬错）/ warnings（验证器侧限制导致的跳过）
```

`_combo_accepted` 是单次探测的原语，它的两段结构是理解整个 parity 的钥匙：

- 先 `inspect.signature(validate_fn).bind(mock_self, **tensors)`——**只绑定，不执行**。绑定失败（`TypeError`）= 签名不匹配，归类为 `signature` 类错误（硬错）。这一步把「签名错」与「函数体里抛错」分开，避免把函数体里的合法 `TypeError`（如 dtype 比较引发的）误报成签名错。
- 绑定 OK 后才 `validate_fn(mock_self, **tensors)` 真正调用。抛 `ValueError`/`TypeError` = 合法拒绝；抛其他异常 = 实现缺陷（硬错）；不抛 = 接受。

#### 4.3.3 源码精读

先看单次探测原语 `_combo_accepted`，重点看 `inspect.signature(...).bind(...)` 这一步——它就是「显式命名参数」约定的消费端：

[scripts/validate_manifest.py:2336-2422](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L2336-L2422) — `_combo_accepted`：构造 mock_self（注入 `self.dtype` 等），先 `inspect.signature(validate_fn).bind(mock_self, **tensors)` 验签名，再调函数体观察接受/拒绝。

注意第 2401 行：`inspect.signature(validate_fn).bind(mock_self, **tensors)`，其中 `tensors` 是 `{input_name: mock_tensor}`。**这要求 `validate_fn` 的形参名恰好是 manifest 的 input 名**——这正是 codegen 用 `exec` 拼出 `def _validate_dtypes(self, a, b):` 而非 `def _validate_dtypes(self, **kwargs):` 的根本原因。若 codegen 用 `**kwargs`，`inspect.signature` 报告的形参就是 `(self, **kwargs)`，`.bind(mock_self, a=..., b=...)` 虽然技术上能绑进 kwargs，但：

1. parity 探测无法再区分「签名与 manifest 对齐」与「随便收一堆 kwargs」——`**kwargs` 体永远 bind 成功，签名错就探测不到了。
2. 退一步，即便用 wrapper 做 per-call `Signature.bind`，也会在 `forward()` 热路径上付出可测开销（codegen 注释明言）。

所以「显式命名参数」是 codegen 与验证器之间的**契约接缝**：codegen 严格按 `signature.inputs` 的键造形参，验证器才能用同一份键去 bind、去探测签名对齐。手写 override 也必须遵守同一形参命名（如 `BmmFp8Op._validate_dtypes(self, a, b, scale_a, scale_b)`），否则 parity 探测会报签名不匹配。

再看接受侧与拒绝侧的入口：

[scripts/validate_manifest.py:2537-2570](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L2537-L2570) — `check_l3_validate_dtypes_parity`：未 override 则发警告并跳过（不静默通过），建议降级 spec-only。

[scripts/validate_manifest.py:2463-2528](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L2463-L2528) — `_probe_out_of_union`（拒绝侧）：从 baseline 出发逐个换越界 sentinel，`self.dtype` 钉在 baseline 主 dtype 只让输入越界，same_as-bound 输入随 ref 传播。

注意第 2387-2394 行 `self.dtype` 的处理：合成/手写体常写 `if x.dtype != self.dtype: raise`，若 mock_self 没有 `self.dtype`，探测定会假阳性拒绝。所以探测前把 `self.dtype` 钉成 baseline 的主 dtype（第一个非 same_as 输入的 dtype），保证只有被探测的输入张量在越界。

现在回到 codegen 侧，看 `maybe_install_validator` 的安装条件——尤其是它对 override 的检测范围：

[tileops/ops/_dtype_codegen.py:322-366](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L322-L366) — `maybe_install_validator`：解析顺序（类附着 `__manifest_*__` → 按 `cls.__name__` 查 manifest）→ `status == "implemented"` 关卡 → 合成并 `cls._validate_dtypes = fn`。

重点看第 345 行的早退条件 `if "_validate_dtypes" in cls.__dict__: return`——它**只看本类 `__dict__`，不沿 MRO 找**。这条规则的注释（本轮 diff 新增）点出了与 roofline codegen 的**关键不对称**：

[tileops/ops/_dtype_codegen.py:336-341](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L336-L341) — 不对称说明：`maybe_install_eval_roofline` 沿 MRO 认 override（家族基类的手写版被保留）；而 `maybe_install_validator` 只认 `cls.__dict__`——家族基类上的手写 `_validate_dtypes` **会被合成版遮蔽**，故手写版必须绑在具体类体内。

这条不对称的后果由一条测试钉死：

[tests/ops/test_pool.py:1584-1593](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_pool.py#L1584-L1593) — `test_pool_codegen_slots_are_class_local`：强制 `eval_roofline` 与 `_validate_dtypes` 必须落在每个**具体类**的 `__dict__` 里，防止家族基类上的定义被合成版静默遮蔽或静默绕过。

> **为什么两条 codegen 不对称？** 这是历史迁移路径的产物（staged-rollout）。roofline codegen 更晚到位，设计成「基类手写版优先」以兼容已有的家族基类；dtype codegen 则要求每个具体类自己持有版本，避免基类与合成版混用导致行为分裂。对算子作者而言，实操结论很简单：**手写 `_validate_dtypes` 就绑在具体类 `__dict__`，别放家族基类**。

最后，这条「类附着 manifest」的旁路用于测试，值得一看：

[tileops/ops/_dtype_codegen.py:348-359](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_dtype_codegen.py#L348-L359) — 解析顺序：先看类属性 `__manifest_signature__` / `__manifest_status__`（测试与想绕过 YAML 加载的调用方用），再按 `cls.__name__` 查 manifest。这让单元测试可以不碰真实 YAML 就驱动 codegen。

#### 4.3.4 代码实践

**实践目标**：把「显式命名参数」与 parity 探测的依赖关系看穿——亲手造一个形参名与 manifest 不符的 `_validate_dtypes`，看验证器怎么报签名不匹配。

**操作步骤**（**源码阅读型实践**，无需 GPU）：

1. 读 [tests/test_validate_manifest.py:158-168](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/test_validate_manifest.py#L158-L168) 的 `_dtype_parity` 辅助函数——它把一个手写 `validate_fn` 装进合成 Op 类，再调 `check_l3_validate_dtypes_parity`。
2. 读 [scripts/validate_manifest.py:2618-2632](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L2618-L2632) 接受侧循环——看 `_combo_accepted` 返回的 `reason` 被归类成 `signature` 时，如何报「`_validate_dtypes` signature does not match manifest inputs」。
3. **思考实验**（不实际跑）：若一个 Op 的 manifest `inputs` 是 `{a, b}`，但作者手写 `_validate_dtypes(self, x, y)`（形参名错了），parity 探测会在哪一步失败？
   - 答：在 `_combo_accepted` 的 `inspect.signature(validate_fn).bind(mock_self, a=..., b=...)` 这步——`bind` 找不到 `a`/`b` 形参会抛 `TypeError`，被归类为 `signature`，报硬错「signature does not match manifest inputs (expected kwargs ['a', 'b'])」。函数体根本不会被执行。

**需要观察的现象**：理解「签名错」与「函数体拒绝」是两个完全不同的失败类别，前者是硬错（实现与 manifest 形参不对齐），后者可能是合法拒绝。

**预期结果**：能用自己的话讲清——codegen 用 `exec` 造显式命名参数、手写版必须用同名形参、验证器靠 `inspect.signature.bind` 同时校验「形参对齐」与「行为对齐」这三者如何通过「input 名」这根线串起来。

#### 4.3.5 小练习与答案

**练习 1**：`_combo_accepted` 为什么要**先** `inspect.signature(...).bind(...)`、**后**真正调用函数体？为什么不直接调用、靠捕获异常来区分？

**参考答案**：因为函数体内部也可能抛 `TypeError`（比如比较不兼容的 torch dtype），若直接调用，就无法区分「形参不匹配的 `TypeError`」与「函数体里 dtype 比较的 `TypeError`」。先 `bind`（只验签名、不执行体）能把签名层面的不匹配单独隔离出来归类为 `signature` 硬错；绑定通过后再调用，此时任何 `ValueError`/`TypeError` 都是函数体的合法拒绝。这种「先验形参、再观行为」的两段式是 parity 探测准确归类的关键。

**练习 2**：`maybe_install_validator` 只看 `cls.__dict__`、不沿 MRO 找 override；`maybe_install_eval_roofline` 却沿 MRO 认 override。如果一个家族基类 `PoolBase` 手写了 `_validate_dtypes`，而具体类 `MaxPool2dFwdOp` 没有自己定义，会发生什么？怎么修？

**参考答案**：`maybe_install_validator(MaxPool2dFwdOp)` 会在 `MaxPool2dFwdOp.__dict__` 找不到 `_validate_dtypes`（它只继承自 `PoolBase`），于是不早退，继续按 `MaxPool2dFwdOp` 的 manifest 合成一个版本并 `cls._validate_dtypes = fn`——**合成版遮蔽了 `PoolBase` 的手写版**。这正是 `test_pool_codegen_slots_are_class_local` 要防的：它要求每个具体类 `__dict__` 里必须有 `_validate_dtypes`。修法是把 `PoolBase` 的手写版下移到每个具体类体内（或让具体类各自合成），不要把 dtype 校验放在家族基类。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个端到端的小任务：

**任务**：为一个假想算子 `FooOp` 完整走一遍「声明 → codegen → parity」。

**Step 1 — 写 manifest signature**。假设 `FooOp` 有两个输入 `x` 与 `scale`，语义是：`x` 只能是 `float16` 或 `bfloat16`；`scale` 必须 `same_as(x)`；此外只允许 `(fp16, fp16)` 这一种跨张量组合（`bf16` 暂未支持）。写出 `signature`：

```yaml
FooOp:
  status: implemented
  signature:
    inputs:
      x: {dtype: "float16 | bfloat16"}
      scale: {dtype: "same_as(x)"}
    dtype_combos:
      - {x: float16, scale: float16}
```

**Step 2 — 手推 codegen 产物**。不跑代码，先在纸上写出 `synthesize_validate_dtypes("FooOp", sig)` 应合成出的 `per_input`、`combo_keys`，以及函数体的等价 Python（形参应为 `(self, x, scale)`）。

参考：

```python
per_input = {
    "x":     ([torch.float16, torch.bfloat16], [],      "float16 | bfloat16"),
    "scale": ([],                          ["x"],       "same_as(x)"),
}
combo_keys = {(torch.float16, torch.float16)}  # 按 input_names=["x","scale"] 取值

# 等价函数体（语义）：
def _validate_dtypes(self, x, scale):
    # x: 在 {fp16,bf16} 内即可
    if x.dtype not in (torch.float16, torch.bfloat16):
        raise ValueError("FooOp: input 'x' has dtype ..., expected 'float16 | bfloat16'")
    # scale: 必须 same_as(x)
    if scale.dtype != x.dtype:
        raise ValueError("FooOp: input 'scale' has dtype ..., expected 'same_as(x)'")
    # combos: 只允许 (fp16, fp16)
    if (x.dtype, scale.dtype) != (torch.float16, torch.float16):
        raise ValueError("FooOp: dtype combination ... is not listed in signature.dtype_combos")
```

注意 `scale` 单张量校验会接受 `(bf16, bf16)`（满足 `same_as(x)`），但** combos 校验**会在末尾把它拒掉——这正是 `dtype_combos` 作为「跨张量子集」约束压过单张量 `same_as` 的地方。

**Step 3 — 验证你的手推**。跑 `synthesize_validate_dtypes("FooOp", sig)`，用 `(fp16, fp16)`、`(bf16, bf16)`、`(fp16, bf16)` 三组输入探测，确认只有第一组通过。

**Step 4 — 连回 parity**。用自己的话解释：验证器 L3 对 `FooOp` 会做哪些探测？（接受侧：`(fp16, fp16)` 必须被接受；拒绝侧：把 `x` 换成越界 dtype 如 `float32` 必须被拒、`scale` 因 `same_as(x)` 随 `x` 传播。）为什么验证器能顺利 `bind`？因为 codegen 造的形参名 `(self, x, scale)` 与 manifest `inputs` 键一致。

> **待本地验证**：Step 3 的报错文案与 Step 2 的等价体措辞以本地 `synthesize_validate_dtypes` 实际产出为准；语义（哪几组通过/被拒）应与上述一致。

## 6. 本讲小结

- `_validate_dtypes` 是 L2 主机侧的纯 dtype 校验；`synthesize_validate_dtypes` 把 manifest `signature.inputs` 的 dtype 表达式**编译成一段纯 Python 函数体**，用 `exec` 物化，合成期把所有解析做满、运行期函数体只做集合查找与引用比较。
- 四种 dtype 构造——普通 token、`|` 并集、`same_as(ref)`、`same_as` 嵌在并集里——由 `_parse_tokens`/`_classify_tokens` 在合成期切成 `(concrete[], refs[])`；`dtype_combos`（R6）由 `_parse_dtype_combos` 归一化成 `combo_keys` 集合，缺省即笛卡尔积全合法。
- **显式命名参数是 codegen 与验证器之间的契约接缝**：codegen 用 `exec` 拼出 `def _validate_dtypes(self, a, b):`，形参严格镜像 `signature.inputs` 键；验证器 L3 靠 `inspect.signature(fn).bind(mock_self, **tensors)` 既校验形参对齐、又观察接受/拒绝行为。
- L3 parity 探测分接受侧（声明的组合必须被接受）与拒绝侧（`_probe_out_of_union` 越界组合必须被拒），对 codegen 合成版与手写 override 一视同仁；`self.dtype` 钉在 baseline 主 dtype 以避免假阳性。
- `maybe_install_validator` 只看 `cls.__dict__`（不沿 MRO），与 `maybe_install_eval_roofline` 沿 MRO 认 override 形成**关键不对称**；后果是手写 `_validate_dtypes` 必须绑在具体类体内，由 `test_pool_codegen_slots_are_class_local` 钉死。
- `status: spec-only` 是 codegen 总开关；非 `implemented` 一律跳过合成、保留 L1 抛 `NotImplementedError` 的 stub——信任模型要求逐 op、逐 PR 迁移。

## 7. 下一步学习建议

- **横向对照 roofline codegen**：读 [u8-l2 Roofline 代码生成](u8-l2-roofline-codegen.md)（若已生成）或直接读 [tileops/ops/_roofline_codegen.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_roofline_codegen.py)，对比它与本讲的异同——同样用「合成期解析 + 运行期直跑」，但 roofline 走 AST 校验而非 `exec`，且 MRO 处理相反。
- **纵向深入信任模型**：读 [u9-l1 四阶段信任模型](u9-l1-trust-model-stages.md) 与 [u9-l2 验证器的五级检查](u9-l2-validator-five-levels.md)，把本讲的 L3 parity 放回 L0–L4 全景，理解 manifest→test→implementation→benchmark 四阶段如何各自守一段。
- **读一个真实手写 override**：精读 [tileops/ops/bmm.py:203](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/bmm.py#L203) 的 `BmmFp8Op._validate_dtypes`，体会「手写版与合成版承担同样 parity 契约」的真实写法，并对照 [tileops/manifest/bmm.yaml:56](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/bmm.yaml#L56) 的 `dtype_combos` 理解声明与执行的分工。
- **写一个新算子的 dtype 校验**：参照 [docs/design/ops-design.md](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md) Step 5，为一个 `status: implemented` 的算子确认其 `_validate_dtypes` 是 codegen 合成还是手写，并跑一次 `python scripts/validate_manifest.py --check-op <Name>` 看 L3 parity 输出。
