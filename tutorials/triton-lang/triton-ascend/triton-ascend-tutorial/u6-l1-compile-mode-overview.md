# compile_mode 三种模式与编译分流

## 1. 本讲目标

本讲是「SIMD 与 SIMT 双编译路径」单元的第一讲。读完本讲，你应当能够：

- 说出 `simd` / `unstructured_in_simt` / `simt_only` 三种 `compile_mode` 各自的语义与编译路径差异；
- 读懂 `NPUOptions.__post_init__` 如何由用户传入的一个 `compile_mode` 字符串，派生出 `force_simt_only`、`force_simt_template`、`parallel_mode` 等内部字段；
- 解释 `add_stages` 如何用 `force_simt_only` 决定编译阶段集合（决定是否跳过 Linalg 主线）；
- 理解默认模式 `unstructured_in_simt` 的「混合」语义：结构化访存留在 SIMD，离散访存尽量走 SIMT 快路径；
- 看懂 `parallel_mode` 这个字段如何在运行时（launcher）侧与启动 API 耦合。

本讲是 u3-l2（`AscendBackend` 阶段注册与 `NPUOptions`）和 u4-l1（`ttir_to_linalg` pass 编排总览）的延伸：前两讲建立了「选项→阶段流水线」的骨架，本讲专门回答「用户改一个 `compile_mode`，编译器内部到底改了什么」。

## 2. 前置知识

在进入本讲前，先用三段话建立必要的直觉。

**SIMD 与 SIMT 是两种并行执行模型。** SIMD（Single Instruction Multiple Data）是一条指令同时处理一组数据，昇腾 NPU 的 Vector/Cube 计算单元天然是 SIMD，要求访存是「连续、对齐、可成块搬运」的结构化（structured）模式。SIMT（Single Instruction Multiple Thread）则是多线程各自独立执行，能天然处理「地址由数据算出、不连续、无规律」的非结构化（unstructured / discrete）访存——例如 `tl.load` 的指针由一个索引张量间接算出。GPU 的 CUDA 就是 SIMT；昇腾 950（A5）在 SIMD 之外新增了 SIMT 能力，专门用来加速这类「离散/间接」访存。

**离散访存是 SIMD 的痛点。** 当一个 `tl.load` 的掩码非连续、或指针来自间接索引，SIMD 单元无法一次性成块搬运，编译器只能把它展开成一串标量循环（scalar loop）——慢且费指令。SIMT 路径则可以用一条 `indirect_load` 指令完成，快很多。这就引出了一个问题：什么时候走 SIMD、什么时候走 SIMT？

**`compile_mode` 就是回答这个问题的用户旋钮。** 它是 `@triton.jit` kernel 启动时的一个关键字参数，例如 `kernel[grid](..., compile_mode="simt_only")`。它本身不直接改任何 IR，而是被翻译成一组内部字段，进而控制编译阶段集合与若干 pass 的行为。本讲要精读的就是这条「`compile_mode` → 内部字段 → 编译路径」的派生链。

> 术语提示：本讲反复出现的 `950` / `compile_on_910_95` 指代昇腾 Atlas A5（910_95 / 950 代）平台。SIMT 能力是 950 代才引入的，因此 SIMT 相关分支只在 950 上真正生效，这一点会在后文反复出现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `third_party/ascend/backend/compiler.py` | 本讲主战场。定义 `NPUOptions`（含 `compile_mode`/`parallel_mode`/`force_simt_*`）、`__post_init__` 字段派生、`AscendBackend.add_stages` 阶段注册、`ttir_to_npubin`（纯 SIMT 路径）、`ttir_to_linalg`（SIMD/混合路径的 pass 编排）。 |
| `third_party/ascend/backend/driver.py` | 运行时侧。读取 `parallel_mode` 决定是否用 950 SIMT 启动 API（`enable_simt`）。 |
| `docs/en/architecture_design_and_core_features.md` | 架构文档第 3.2.3 节「SIMT Compiler」，给出三种模式的总览表与编译流程图，是本讲语义的权威说明。 |
| `third_party/ascend/unittest/autotune_ut/test_reduce_simt.py` | 一个真实用例：以 `compile_mode='simt_only'` 启动 reduce kernel，演示该参数的实际写法。 |
| `third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp` 与 `third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp` | `force_simt_template` 在 C++ pass 内部的真正消费者，决定是否把离散访存「打标记让权给 SIMT」。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：4.1 讲 `compile_mode` 本身（用户旋钮与三种模式语义）；4.2 讲 `force_simt_only` / `force_simt_template` 这两个派生字段如何分叉编译路径；4.3 讲 `parallel_mode` 字段及其在启动侧的耦合。

### 4.1 compile_mode：用户面向的三种编译模式

#### 4.1.1 概念说明

`compile_mode` 是一个面向用户的字符串选项，取值有三种：

- `"simd"`：纯 SIMD。结构化访存走 DMA 成块搬运；离散/非结构化访存展开成标量循环。
- `"unstructured_in_simt"`（默认）：混合模式。结构化访存仍留在 SIMD；离散访存在 950 上尽量走 SIMT 模板（`indirect_load`/`indirect_store`），失败再回退到标量循环。
- `"simt_only"`：纯 SIMT。把 Triton IR 直接送给 AscendNPU IR 做纯 SIMT 编译，跳过 Linalg 主线。

注意它「本身不改 IR」。它是一个编译期路由开关：决定 kernel 走哪条编译流水线、哪些 pass 开启 SIMT 行为。这正是本讲的中心论点——**一个字符串 → 一组内部字段 → 不同的编译路径**。

#### 4.1.2 核心流程

三种模式与编译路径的对应关系（来自架构文档）：

| `compile_mode` | 语义 | 编译路径 |
|---|---|---|
| `"simd"` | 纯 SIMD | `Triton IR → Linalg IR → AscendNPU IR` |
| `"unstructured_in_simt"`（默认） | 混合：结构化留 SIMD，离散优先 SIMT 模板 | `Triton IR → Linalg IR → AscendNPU IR` |
| `"simt_only"` | 纯 SIMT，直接喂 Triton IR | `Triton IR → AscendNPU IR`（跳过 Linalg） |

用伪代码描述三者在阶段集合上的分叉（对应 `add_stages` 的逻辑）：

```text
读 compile_mode
if mode == "simt_only":          # 纯 SIMT
    阶段 = ttir → npubin          # ttir_to_npubin，立即返回，跳过 Linalg
else:                            # simd 或 unstructured_in_simt
    阶段 = ttir → ttadapter → (mlirbc→bcmlir?) → npubin
    其中 ttadapter = ttir_to_linalg（含完整 Ascend pass 链）
```

`simd` 与 `unstructured_in_simt` 走的**阶段集合完全相同**，它们的区别只在 `ttir_to_linalg` 内部若干 pass 的行为参数上（详见 4.2）。而 `simt_only` 是阶段集合层面的硬分叉。

#### 4.1.3 源码精读

`compile_mode` 作为 `NPUOptions` 的一个字段定义在 compiler.py，默认值是 `"unstructured_in_simt"`：

[third_party/ascend/backend/compiler.py:1088-1091](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1088-L1091) — 字段定义。注意第 1088 行的注释 `# compile_mode: "simd" (default), ...` 与第 1090 行的实际默认值 `"unstructured_in_simt"` **不一致**：这是源码中一处滞后的注释，真实默认以第 1090 行字段值和架构文档为准。

```python
    # compile_mode: "simd" (default), "unstructured_in_simt", "simt_only"
    # When compile_mode is provided, it automatically sets other fields
    compile_mode: str = "unstructured_in_simt"
    mix_mode: str = ""
```

> 这个注释与默认值不符的小细节是真实存在的源码现象。读源码时若发现「注释说 simd 是默认，但文档和实际跑起来都是 unstructured_in_simt」，应以字段定义与官方文档为准。养成「注释只是线索、以可执行代码为准」的习惯很重要。

`compile_mode` 是怎么传进来的？用户在启动 kernel 时把它当作普通关键字参数传入，例如：

[third_party/ascend/unittest/autotune_ut/test_reduce_simt.py:68-74](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/autotune_ut/test_reduce_simt.py#L68-L74) — 真实用例，`compile_mode='simt_only'` 作为启动参数传入。这个 reduce kernel 含间接索引访存（`tl.load(in_ptr0 + x1_numel * y0 + x1, ...)`），正是 SIMT 的用武之地，测试还用 `@pytest.mark.skipif(not is_compile_on_910_95(), ...)` 标明 SIMT 仅在 A5/950 上可用。

```python
    triton_unk_reduce[(grid_size, 1, 1)](
        arg0_1,
        buf44,
        y0_numel,
        x1_numel,
        compile_mode='simt_only',
    )
```

这些启动关键字参数会经 core 的 `JITFunction.run → compile` 流到后端的 `parse_options`。`parse_options` 用白名单方式过滤——只接受 `NPUOptions` 已声明的字段，`compile_mode` 正是其中之一：

[third_party/ascend/backend/compiler.py:1213-1232](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1213-L1232) — 关键是第 1216 行 `{k: opts[k] for k in NPUOptions.__dataclass_fields__.keys() if k in opts}`：只把 `NPUOptions` 认识的字段挑出来构造选项对象，随后触发 `__post_init__`（见 4.2）。同时这里还做了 `compile_on_910_95`、`enable_dynamic_cv_pipeline` 等字段的懒初始化。

```python
    def parse_options(self, opts) -> Any:
        if self.target.backend == "npu":
            args = {k: opts[k] for k in NPUOptions.__dataclass_fields__.keys() if k in opts}
            args.setdefault("arch", self.target.arch)
            options = NPUOptions(**args)
            ...
```

架构文档给出了三种模式的权威说明与编译流程图，是理解语义的最佳入口：

[docs/en/architecture_design_and_core_features.md:214-233](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/architecture_design_and_core_features.md#L214-L233) — `compile_mode` 总览表与三种用法示例。文档明确：默认是 `unstructured_in_simt`，且 SIMT 路径是 950 代新增能力。

文档还用一张表总结了三种模式在「离散掩码处理 / 非结构化访存 / TritonToLinalg」三个阶段的差异，值得逐行对照：

[docs/en/architecture_design_and_core_features.md:275-280](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/architecture_design_and_core_features.md#L275-L280) — 三模式阶段差异表。`simt_only` 列三行全是「Not run」，印证了它跳过 Linalg 主线。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「`compile_mode` 是一个被 `parse_options` 接收的合法字段」，并理解它的写法。

**操作步骤**（源码阅读型，无需设备）：

1. 打开 `third_party/ascend/backend/compiler.py`，定位 `NPUOptions` 的 `compile_mode` 字段（约 1090 行），确认默认值。
2. 打开 `third_party/ascend/unittest/autotune_ut/test_reduce_simt.py`，看第 73 行如何把 `compile_mode='simt_only'` 传进 kernel 调用。
3. 在 compiler.py 的 `parse_options`（约 1216 行）确认白名单逻辑：试着推断如果用户传了一个 `NPUOptions` 不认识的字段（例如 `compile_mode="typo_mode"`），会发生什么。

**需要观察的现象**：

- `compile_mode` 是 dataclass 字段，所以白名单会放行它，值原样进入 `NPUOptions(**args)`。
- 但 `"typo_mode"` 既不是 `simd`/`unstructured_in_simt`/`simt_only`，`__post_init__` 里的三个 `if/elif` 都不命中，于是它**不会派生任何字段**——相当于一个静默无效的模式。这是一个容易踩的坑：拼错模式名不会报错，只是不生效。

**预期结果**：你能口述「`compile_mode` 经启动参数 → `parse_options` 白名单 → `NPUOptions` 构造 → `__post_init__` 派生」这条链路，并知道拼错模式名会被静默忽略。（注：实际运行行为待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：如果用户既不传 `compile_mode` 也不传 `force_simt_template`，最终走的是哪种模式？
**答案**：默认 `compile_mode="unstructured_in_simt"`，`__post_init__` 会把 `force_simt_template` 置为 `True`。即默认就是混合模式。

**练习 2**：为什么 `simt_only` 的文档说它「直接把 Triton IR 送给 AscendNPU IR」，而不是先过 Linalg？
**答案**：因为 SIMT 执行模型能直接理解 Triton 的逐线程语义（含间接/离散访存），不需要先把访存「结构化」成 Linalg 的成块 DMA 形式。强行先过 Linalg 反而会破坏 SIMT 想保留的非结构化信息。

**练习 3**：把 `compile_mode="simd"`（注释里标注的「默认」）和真正的默认 `"unstructured_in_simt"` 对比，二者编译阶段集合一样吗？
**答案**：一样。二者都走 `ttir → ttadapter → npubin`，区别只在 `ttadapter`（即 `ttir_to_linalg`）内部 `force_simt_template` 的取值不同（见 4.2）。

---

### 4.2 force_simt_only / force_simt_template：编译路径的分叉

#### 4.2.1 概念说明

`compile_mode` 是给用户看的；编译器内部真正用来「分叉」的是三个派生字段：

- `force_simt_only`（布尔）：为真 → 走纯 SIMT 路径 `ttir_to_npubin`，**跳过整个 Linalg 主线**。这是「阶段集合层面」的硬分叉。
- `force_simt_template`（布尔）：为真 → 在 SIMD 主线（`ttir_to_linalg`）内部，让离散掩码 pass 与非结构化访存 pass 启用 SIMT 模板路由（打标记 / 转 `indirect_load`）。这是「pass 行为层面」的软开关。
- `parallel_mode`（字符串）：记录并行执行模型（`"simd"` / `"simt"` 等），在启动侧决定是否用 950 SIMT 启动 API（详见 4.3）。

这三个字段默认值都不为「启用」：`force_simt_only=False`、`force_simt_template=False`、`parallel_mode="simd"`。它们由 `__post_init__` 根据 `compile_mode` 设置。

#### 4.2.2 核心流程

`__post_init__` 的派生规则（精确对应源码）：

```text
compile_mode == "simd":
    parallel_mode = "simd"
    （force_simt_template 保持默认 False）

compile_mode == "unstructured_in_simt":   # 默认
    force_simt_template = True
    （parallel_mode 保持默认 "simd"）

compile_mode == "simt_only":
    force_simt_only = True
    parallel_mode = "simt"

随后（与模式无关）：
    if force_simt_only: shared_mem_dynamic_size = 122880   # 若未显式给定
    else:               shared_mem_dynamic_size = 221184
```

派生之后，这些字段如何分流编译？关键在两处消费点：

1. **`add_stages` 读 `force_simt_only`** → 决定注册哪些阶段（4.2.3 第一段）。
2. **`ttir_to_linalg` 读 `force_simt_template`** → 把它作为参数传给离散掩码 pass 与非结构化访存 pass（4.2.3 第二段）。

一句话总结：`force_simt_only` 控制「走不走 Linalg」，`force_simt_template` 控制「在 Linalg 路径里要不要启用 SIMT 模板」。

#### 4.2.3 源码精读

先看字段派生。`__post_init__` 是 `NPUOptions` 这个 frozen dataclass 的钩子方法。因为 dataclass 被声明为 `frozen=True`，普通赋值会抛异常，所以这里统一用 `object.__setattr__` 绕过冻结来改字段：

[third_party/ascend/backend/compiler.py:1111-1126](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1111-L1126) — `__post_init__`。注意第 1116 行注释「For historical compatibility reasons, force_simt_template will still be used」：揭示了 `force_simt_template` 是历史遗留命名，`unstructured_in_simt` 这个新名字在内部仍映射到它。

```python
    def __post_init__(self):
        # Parse compile_mode and set related fields
        if self.compile_mode == "simd":
            object.__setattr__(self, "parallel_mode", "simd")
        elif self.compile_mode == "unstructured_in_simt":
            # For historical compatibility reasons, force_simt_template will still be used.
            object.__setattr__(self, "force_simt_template", True)
        elif self.compile_mode == "simt_only":
            object.__setattr__(self, "force_simt_only", True)
            object.__setattr__(self, "parallel_mode", "simt")

        if self.force_simt_only:
            if self.shared_mem_dynamic_size is None:
                object.__setattr__(self, "shared_mem_dynamic_size", 122880)
        else:
            object.__setattr__(self, "shared_mem_dynamic_size", 221184)
```

字段本身的声明（注意默认值）：

[third_party/ascend/backend/compiler.py:1079-1091](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1079-L1091) — `parallel_mode` 默认 `"simd"`、`force_simt_only` 默认 `False`、`force_simt_template` 默认 `False`、`compile_mode` 默认 `"unstructured_in_simt"`。对照 `__post_init__` 即可还原三种模式的派生结果。

```python
    parallel_mode: str = "simd"
    force_simt_only: bool = False
    force_simt_template: bool = False
    ...
    compile_mode: str = "unstructured_in_simt"
```

**消费点一：`add_stages` 的阶段分叉。** 这是 `force_simt_only` 唯一的、也是决定性的消费点：

[third_party/ascend/backend/compiler.py:1269-1287](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1269-L1287) — `add_stages`。第 1272–1274 行：若 `force_simt_only` 为真，只注册 `ttir` 与 `npubin`（实现是 `ttir_to_npubin`）后**立即 return**，彻底跳过 `ttadapter`/`mlirbc`/`bcmlir`。否则注册完整的 SIMD/混合阶段链。

```python
    def add_stages(self, stages, options, language):
        if self.target.backend == "npu":
            stages["ttir"] = lambda src, metadata: make_ttir(src, metadata, options)
            if options.force_simt_only:
                stages["npubin"] = (lambda src, metadata: ttir_to_npubin(src, metadata, options))
                return
            stages["ttadapter"] = lambda src, metadata: ttir_to_linalg(src, metadata, options, named_ops=True)
            ...
```

**纯 SIMT 路径 `ttir_to_npubin` 内部长什么样？** 它把 Triton IR 直接喂给 BiSheng 编译器，并带上一组纯 SIMT 专用选项：

[third_party/ascend/backend/compiler.py:1147-1164](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1147-L1164) — `if opt.force_simt_only:` 分支。`--enable-hivm-compile=false`（关闭 SIMD 的 hivm 编译）、`--enable-triton-ir-compile`（启用 Triton IR 直编）、`--pure-simt`（声明纯 SIMT）、并带上 `--num-warps` / `--threads-per-warp` 等 SIMT 执行模型参数。注意它用 `_parse_ttir_metadata`（而非 `_parse_linalg_metadata`），且把 `mix_mode` 固定为 `"aiv"`（因为纯 SIMT 只用向量核）。

```python
        if opt.force_simt_only:
            _compile_option_list += ["--enable-hivm-compile=false"]
            _compile_option_list += ["--enable-triton-ir-compile"]
            _compile_option_list += ["--pure-simt"]
            _compile_option_list += [f"--num-warps={opt.num_warps}"]
            _compile_option_list += [f"--threads-per-warp={opt.warp_size}"]
            ...
```

**消费点二：`ttir_to_linalg` 内的 `force_simt_template`。** 这是 `simd` 与 `unstructured_in_simt` 唯一的区别点。`force_simt_template` 从 metadata 取出，作为参数传给两个离散/非结构化访存 pass：

[third_party/ascend/backend/compiler.py:204-207](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L204-L207) — 第 180 行先从 metadata 读 `force_simt_template`，再在第 204、207 行作为第 3 个实参传给 `add_discrete_mask_access_conversion` 和 `add_triton_to_unstructure`。

```python
        force_simt_template = metadata["force_simt_template"]
        ...
        ascend.passes.ttir.add_discrete_mask_access_conversion(pm, compile_on_910_95, force_simt_template,
                                                               enable_sync_block_lock)
        ...
        ascend.passes.ttir.add_triton_to_unstructure(pm, compile_on_910_95, force_simt_template)
```

`force_simt_template` 传到 C++ 后做什么？以离散掩码 pass 为例，它只在「950 + force_simt_template + 秩 ≤ 5」三者同时满足时，给离散访存 op 打一个 `route_discrete_mask_to_simt` 标记后**立刻返回 failure（不改写 IR）**，把处理权让给下游的非结构化访存 pass：

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:281-287](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L281-L287) — 三个条件门控：`compileOn91095Flag && forceSimtTemplateFlag && rankWithinIndirectFastPathLimit`。注意它只是 `setAttr(...routeDiscreteMaskToSimt...)` 后 `return failure()`，即「打标记、让权」，而非当场改写。

```cpp
    bool rankWithinIndirectFastPathLimit =
        ptrType && ptrType.getShape().size() <= 5;
    if (compileOn91095Flag && forceSimtTemplateFlag &&
        rankWithinIndirectFastPathLimit) {
      op->setAttr(routeDiscreteMaskToSimtAttrName, rewriter.getUnitAttr());
      return failure();
    }
```

下游非结构化访存 pass 则真正把 `load/store` 改写成 SIMT 的 `indirect_load/indirect_store`（同样是三条件门控）：

[third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp:532-542](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L532-L542) — `indirectFastPathEnabled = compileOn91095Flag && forceSimtTemplateFlag && (...)`。条件不满足则回退到标量循环路径（与纯 `simd` 一致）。

```cpp
  // SIMT Indirect Fast-Path Lowering in 950 seiries
  bool indirectFastPathEnabled =
      compileOn91095Flag && forceSimtTemplateFlag &&
      ((!ptrOffsetInfo.isStructured() && sizeInByte < 64) ||
       routeDiscreteMaskToSimt);
  ...
  if (indirectFastPathEnabled &&
      succeeded(tryRewriteIndirectFastPath(...))) {
    return success();
  }
```

> 关键洞察：`unstructured_in_simt` 的「混合」语义就体现在这里——**它不是把整个 kernel 搬到 SIMT**，而是逐个访存点判断：结构化的留 SIMD，离散的（且满足 950 + 秩 ≤ 5）走 SIMT 模板，不满足的回退标量循环。这也是架构文档「Hybrid Mode: SIMT Only for Discrete Access」一节的含义（[docs/en/architecture_design_and_core_features.md:281-293](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/architecture_design_and_core_features.md#L281-L293)）。

最后，这些派生字段都会进入缓存键，保证换模式必重编：

[third_party/ascend/backend/compiler.py:1128-1131](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1128-L1131) — `hash()` 把 `self.__dict__`（含 `force_simt_*`、`parallel_mode`、`compile_mode`）全部拼进缓存键。所以同一个 kernel 用不同 `compile_mode` 编译，缓存键不同，不会串用。

#### 4.2.4 代码实践

**实践目标**：用同一个 kernel 分别以 `simd` 与 `unstructured_in_simt`（默认）编译，比较 dump 出来的 pass 流程差异；再以 `simt_only` 编译，确认阶段集合的硬分叉。

**操作步骤**：

1. 准备一个含**非结构化/离散访存**的 kernel（用 `test_reduce_simt.py` 里的 `triton_unk_reduce` 即可，它的 `tl.load(in_ptr0 + x1_numel * y0 + x1, ...)` 是典型的间接访存）。一个纯结构化的 vector-add 在两种模式下 **IR 完全相同**，看不出差异——这正是要体会的点。
2. 开启调试与强制重编：`export TRITON_DEBUG=1`、`export TRITON_ALWAYS_COMPILE=1`，必要时 `rm -rf ~/.triton/cache`。
3. 以三种模式分别启动同一个 kernel：
   ```python
   kernel[grid](..., compile_mode="simd")
   kernel[grid](...)                       # 默认 unstructured_in_simt
   kernel[grid](..., compile_mode="simt_only", num_warps=32)
   ```
4. 观察 `ttir_to_linalg` 在 debug 下打印的 `[DEBUG] cmd list:`（含 `--pass-pipeline=...`），对比 `discrete-mask-access-conversion` 与 `triton-to-unstructured` 两个 pass 携带的 `force_simt_template` 实参。

**需要观察的现象**：

- `simd` vs `unstructured_in_simt`：阶段集合相同（都有 `ttir→ttadapter→...→npubin`），区别只在上述两个 pass 的 `force_simt_template` 参数（`False` vs `True`）。在 950 设备上，含离散访存的 kernel 在 `unstructured_in_simt` 下应出现 `indirect_load/indirect_store`，而 `simd` 下应展开成标量循环。
- `simt_only`：dump 中**看不到** `ttadapter`/`mlirbc`/`bcmlir` 阶段，只有 `ttir` 与 `npubin`，且 `npubin` 的命令行带 `--pure-simt --enable-triton-ir-compile`，印证了 `add_stages` 的 early return。
- 非结构化的 vector-add：`simd` 与 `unstructured_in_simt` 的生成 IR 无差异（`force_simt_template` 只对离散访存起作用）。

**预期结果**：你能画出三种模式各自的「阶段序列 + 关键 pass 实参」对照表。注：精确的 dump 文本与 `indirect_load` 是否出现依赖 950 硬件，**待本地验证**；在没有 950 的环境下，`force_simt_template=True` 也不会触发 SIMT 快路径（因为 `compileOn91095Flag` 为假），行为退化成与 `simd` 一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `force_simt_template` 要用 `object.__setattr__` 而不是直接 `self.force_simt_template = True`？
**答案**：`NPUOptions` 是 `@dataclass(frozen=True)`，直接赋值会抛 `FrozenInstanceError`。`object.__setattr__` 绕过 dataclass 的冻结保护来改字段，这是 frozen dataclass 里做派生初始化的惯用法。

**练习 2**：在非 950（如 A2/A3）平台上，设 `compile_mode="unstructured_in_simt"` 和 `compile_mode="simd"`，生成的 IR 会有区别吗？
**答案**：不会有实际区别。虽然 `force_simt_template` 一为 `True` 一为 `False`，但 SIMT 快路径的 C++ 门控还要求 `compileOn91095Flag` 为真；非 950 平台该旗标为假，快路径不激活，两者都退化为标量循环。

**练习 3**：`simt_only` 模式下，`ttir_to_linalg` 会被调用吗？为什么？
**答案**：不会。`add_stages` 在 `force_simt_only` 为真时只注册 `ttir` 和 `npubin`（`ttir_to_npubin`）后立即 `return`，根本不注册 `ttadapter` 阶段，core 自然不会调用 `ttir_to_linalg`。

---

### 4.3 parallel_mode：并行模型字段与启动侧耦合

#### 4.3.1 概念说明

`parallel_mode` 是一个字符串字段，记录 kernel 的并行执行模型。它在 `NPUOptions` 里的取值主要是 `"simd"` 与 `"simt"`，但在走 Linalg 路径时，`triton-to-linalg` pass 还会把一个更细的并行模式（如 `"mix_simd_simt"`）写进 IR，再由 `_parse_linalg_metadata` 解析回 metadata 覆盖该字段。

它的作用不在编译期分流（那是 `force_simt_*` 的职责），而在**运行期启动**：launcher（`driver.py`）读 `parallel_mode` 来决定用哪条 CANN 启动 API。可以说 `force_simt_*` 管「怎么编」，`parallel_mode` 辅助管「怎么启动」。

#### 4.3.2 核心流程

`parallel_mode` 的取值流转：

```text
NPUOptions 字段默认值:  parallel_mode = "simd"
__post_init__ 派生:
    compile_mode == "simd"                → parallel_mode = "simd"
    compile_mode == "unstructured_in_simt"→ parallel_mode 保持 "simd"（不改）
    compile_mode == "simt_only"           → parallel_mode = "simt"

走 Linalg 路径时（非 simt_only）:
    triton-to-linalg 把真实并行模式写进 IR（如 "mix_simd_simt"）
    _parse_linalg_metadata 用正则从 IR 解析并覆盖 metadata["parallel_mode"]

启动侧（driver.py）:
    parallel_mode = metadata.parallel_mode
    enable_simt = ("simt" in parallel_mode) or force_simt_only
    enable_simt → 选择 950 SIMT 启动 API（rtKernelLaunchWithFlagV2）或普通启动
```

注意 `"simt" in parallel_mode` 是子串匹配：`"simt"`、`"mix_simd_simt"` 都会命中，从而开启 SIMT 启动路径。

#### 4.3.3 源码精读

`parallel_mode` 的字段声明与默认值：

[third_party/ascend/backend/compiler.py:1079-1082](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1079-L1082) — `parallel_mode: str = "simd"`，默认 SIMD。紧随其后的 `force_simt_only`/`force_simt_template` 默认都是 `False`。

```python
    parallel_mode: str = "simd"
    force_simt_only: bool = False
    force_simt_template: bool = False
```

`__post_init__` 对 `parallel_mode` 的设置已在上文 4.2.3 引用（第 1114、1120 行）。归纳一下三种模式的 `parallel_mode` 终值：`simd`→`"simd"`、`unstructured_in_simt`→`"simd"`（不改）、`simt_only`→`"simt"`。

Linalg 路径下，IR 中写入的并行模式会被解析覆盖 metadata（这是「混合模式」实际并行模式比选项字段更细的原因）：

[third_party/ascend/backend/compiler.py:375-399](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L375-L399) — `_parse_linalg_metadata` 用 `PARALLEL_MODE_REGEX` 从 Linalg IR 抠出 `parallel_mode`（如 `mix_simd_simt`）写入 metadata，覆盖 `NPUOptions` 里粗粒度的 `"simd"`。也就是说，混合模式下即使选项字段是 `"simd"`，metadata 最终可能变成 `"mix_simd_simt"`。

```python
    # Example: parallel_mode = "mix_simd_simt" -> mix_simd_simt
    PARALLEL_MODE_REGEX = r'parallel_mode\s*=\s*"([^"]+)"'
    ...
    metadata["parallel_mode"] = re.search(PARALLEL_MODE_REGEX, linalg).group(1)
```

启动侧的真正消费点在 `driver.py` 的 `make_launcher`：

[third_party/ascend/backend/driver.py:439-440](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L439-L440) — `enable_simt = ("simt" in parallel_mode) or metadata.force_simt_only`。这个 `enable_simt` 正是 u5-l3 讲过的两条启动 API（普通 `rtKernelLaunch` vs 950 `rtKernelLaunchWithFlagV2`）的分水岭。

```python
    parallel_mode = metadata.parallel_mode
    enable_simt = ("simt" in parallel_mode) or metadata.force_simt_only
```

> 把本讲与 u5-l3 串起来：`compile_mode="simt_only"` → `force_simt_only=True` → `enable_simt=True` → launcher 生成带 `rtKernelLaunchWithFlagV2`（携带 `localMemorySize` 等 SIMT 专属信息）的启动代码。这就是「编译期旋钮」一路传导到「运行期启动 API」的完整链条。

#### 4.3.4 代码实践

**实践目标**：追踪 `parallel_mode` 从选项字段到启动 API 的完整传导，验证它确实影响启动路径。

**操作步骤**（源码阅读 + 可选运行）：

1. 在 compiler.py 找到 `parallel_mode` 默认值（1079 行）与 `__post_init__` 对它的两次设置（1114、1120 行）。
2. 在 `_parse_linalg_metadata`（约 399 行）确认 IR 写入的 `parallel_mode` 会覆盖选项字段。
3. 在 driver.py（439–440 行）确认 `enable_simt` 的计算公式。
4. （可选，需 950 设备）开启 `TRITON_DEBUG=1`，对 `compile_mode="simt_only"` 的 kernel 查看 dump 出的 launcher `.cxx` 源码，定位 `rtKernelLaunchWithFlagV2` 的调用与 `localMemorySize` 参数。

**需要观察的现象**：

- `simt_only` 模式下，launcher 生成的 C++ 里应出现 `rtKernelLaunchWithFlagV2`（因为 `enable_simt=True`）。
- 纯 `simd` 模式且 kernel 不含 SIMT 算子时，`parallel_mode` 为 `"simd"`，`enable_simt=False`，launcher 用普通 `rtKernelLaunch`。

**预期结果**：你能用一句话说清 `parallel_mode` 的作用——「它不直接改 IR，而是作为 metadata 传到 launcher，决定用哪条 CANN 启动 API」。（launcher 源码的具体文本待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`"simt" in parallel_mode` 这种子串判断，对 `parallel_mode="mix_simd_simt"` 会命中吗？这样设计合理吗？
**答案**：会命中（`"simt"` 是 `"mix_simd_simt"` 的子串）。合理——混合模式里只要有 SIMT 成分，就需要 SIMT 启动 API 的能力（如声明 per-block 动态局部存储），所以用子串判断即可覆盖。

**练习 2**：为什么 `unstructured_in_simt` 模式下 `NPUOptions.parallel_mode` 仍是 `"simd"`，而不是某种「混合」值？
**答案**：因为混合模式是否真的产生 SIMT 成分，要等 `triton-to-linalg` 处理完离散访存才知道（取决于是否生成了 `indirect_load` 等）。所以选项字段先留粗粒度的 `"simd"`，真实并行模式由 pass 写进 IR，再由 `_parse_linalg_metadata` 解析覆盖。

**练习 3**：`parallel_mode` 会进入编译缓存键吗？换模式但忘清缓存会串用吗？
**答案**：会进入缓存键（`hash()` 拼了整个 `__dict__`）。所以换 `compile_mode` 必然换键，不会串用，无需手动清缓存（除非调试时想强制重编）。

## 5. 综合实践

**任务**：画一张「`compile_mode` 全链路传导图」，把本讲三个模块串起来，并用一次真实（或源码阅读）编译验证其中一条边。

要求在图中至少标注以下节点与边：

1. 用户传入 `compile_mode`（三种取值）。
2. `parse_options` 白名单放行 → 构造 `NPUOptions` → 触发 `__post_init__`。
3. `__post_init__` 派生 `force_simt_only` / `force_simt_template` / `parallel_mode`（用表格列出三种模式各自的派生结果）。
4. `add_stages` 读 `force_simt_only` → 决定阶段集合（`simt_only` 走 `ttir→npubin`，其余走 `ttir→ttadapter→…→npubin`）。
5. `ttir_to_linalg` 读 `force_simt_template` → 控制离散掩码/非结构化访存 pass 的 SIMT 路由（三条件门控）。
6. `parallel_mode` → `_parse_linalg_metadata` 覆盖 → `driver.py` 的 `enable_simt` → 启动 API 选择。

**验证一条边**：挑「边 4」（阶段集合分叉）做实证——按 4.2.4 的步骤，用 `TRITON_DEBUG=1` 比较 `simt_only` 与默认模式 dump 出的阶段文件名集合，确认前者没有 `kernel.ttadapter.mlir`。

**交付物**：一张传导图（手绘或工具画均可）+ 一段 100 字以内的结论，说明「为什么默认是 `unstructured_in_simt` 而不是 `simd`」（提示：在 950 上对离散访存免费拿到 SIMT 加速，而对纯结构化 kernel 又退化为与 `simd` 完全等价，是风险最低的默认选择）。

> 说明：含 SIMT 效果的实证依赖 950（A5）硬件；在无 950 环境下，本实践退化为「源码阅读 + 阶段文件名对照」，SIMT 相关 IR 差异部分标注「待本地验证」。

## 6. 本讲小结

- `compile_mode` 是用户旋钮，三值 `simd` / `unstructured_in_simt`（默认）/ `simt_only`；它本身不改 IR，只驱动内部字段派生。
- `__post_init__` 把一个字符串派生成 `force_simt_only`、`force_simt_template`、`parallel_mode`，这是「用户语义→编译器内部开关」的唯一桥梁（用 `object.__setattr__` 绕过 frozen dataclass）。
- `force_simt_only` 是阶段集合的硬分叉：为真则 `add_stages` 只注册 `ttir→npubin`（`ttir_to_npubin`，带 `--pure-simt`），跳过整个 Linalg 主线。
- `force_simt_template` 是 pass 行为的软开关：在 `ttir_to_linalg` 内控制离散掩码/非结构化访存 pass 是否启用 SIMT 模板路由（`indirect_load/store`），受「950 + 秩 ≤ 5」门控，不满足则回退标量循环。
- `unstructured_in_simt` 的「混合」语义：逐访存点判断，结构化留 SIMD、离散走 SIMT、不可行回退——不是整 kernel 搬到 SIMT。
- `parallel_mode` 不改 IR，而是经 `_parse_linalg_metadata`（IR 写入值覆盖）传到 `driver.py`，用 `"simt" in parallel_mode or force_simt_only` 算出 `enable_simt`，决定用哪条 CANN 启动 API。

## 7. 下一步学习建议

- 下一讲 **u6-l2「离散访存 SIMT 模板与纯 SIMT 路径」** 会深入本讲只点到为止的两条 SIMT 路径：混合模式下的 `indirect_load/store`、`__builtin_indirect_atomic` 快路径及其秩 ≤ 5 启用条件，以及 `simt_only` 经 `ttir_to_npubin` 的纯 SIMT 编译选项（`--pure-simt`、`--num-warps` 等）。建议先复习本讲 4.2.3 的 C++ 门控代码。
- 若想理解启动侧 `enable_simt` 之后的细节，复习 **u5-l3「内核启动：rtKernelLaunch、workspace 与 sync_block_lock」**，看 `rtKernelLaunchWithFlagV2` 额外携带的 `localMemorySize`。
- 若想从 pass 实现角度理解「为什么需要 SIMT」，复习 **u4-l3（离散掩码）** 与 **u4-l4（非结构化标量化）**，对照 SIMD 下展开标量循环的代价。
- 建议阅读源码：`third_party/ascend/backend/compiler.py` 的 `__post_init__` 与 `add_stages`、`docs/en/architecture_design_and_core_features.md` 第 3.2.3 节，作为本讲的权威参照。
