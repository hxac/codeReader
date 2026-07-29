# 三段式编译流水线 make_ttir / make_tileir / make_cubin

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 TileIR 后端的三个编译阶段 `make_ttir`、`make_tileir`、`make_cubin` 各自的输入、输出和职责。
- 解释这三个阶段是如何被上游 `compile()` 注册并以字典顺序驱动的，以及每个阶段的产物（`.ttir` / `.tileir` / `.cubin`）如何落盘。
- 逐条列出 `make_tileir` 里挂载的 pass 及其**先后顺序**，并理解「为什么有的 pass 必须在转换前、有的必须在转换后」。
- 说清楚 `only_contain_legal_dialects` 这道「合法性校验」检查的是什么、不通过时抛出什么错误、为什么必须放在转换之后。

本讲只读源码、不修改源码。运行类操作需要 Blackwell GPU + CUDA 13.1 工具链，相关现象标注为「待本地验证」。

## 2. 前置知识

在进入本讲前，请确认你已经理解下面几个概念（它们来自前置讲义）：

- **TTIR（Triton IR）**：Triton 自有的 MLIR 方言（`tt.*`），是前端把 Python `@triton.jit` kernel 翻译出来的第一种 IR 形态。PTX 后端和 TileIR 后端**共享这一前端产物**，区别只在 TTIR 之后的走向。
- **cuda_tile 方言**：NVIDIA CUDA Tile IR 使用的方言（`cuda_tile.*`）。`tileiras` 外部编译器只认这个方言的 bytecode。
- **Pass / PassManager**：MLIR 的标准机制。一个 pass 对 IR 做一次特定变换（如内联、规范化、类型转换）；PassManager 按注册顺序依次执行一组 pass。本讲反复出现的 `pm` 就是 PassManager。
- **`ENABLE_TILE=1`**：让 Triton 选中 TileIR 后端的环境开关（见 u2-l1）。本讲讨论的所有代码都在「已选中 TileIR 后端」的前提下运行。
- **`metadata` 与 `opt`**：上游 `compile()` 会把本次 JIT 的旋钮分成两路。`num_warps` / `num_ctas` / `num_stages` 这些「普通字段」会随 `options.__dict__` 流进 `metadata` 字典；而 `enable_approx` / `enable_ftz` 是 `@property`，不在 `__dict__` 里，所以只能通过 `opt`（即 `TileIROptions` 实例）访问。这个分流在本讲的 `make_tileir` 里会再次出现，是理解 pass 选项注入的关键（见 u2-l2）。

一个一句话总览（来自 u1-l4）：`make_ttir` 做 MLIR 通用清理 → `make_tileir` 把 TTIR 转成 cuda_tile 方言并校验 → `make_cubin` 把 bytecode 交给外部 `tileiras`。本讲把这三段拆开讲透。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `third_party/tileir/backend/compiler.py` | TileIR 后端的 Python 主文件。`TileIRBackend` 类定义了 `make_ttir` / `make_tileir` / `make_cubin` / `add_stages` / `call_tileiras`，是本讲的主战场。 |
| `third_party/tileir/triton_tileir.cc` | C++ pybind 插件。把 C++ 的转换 pass 包装成 Python 可调用的 `tileir.passes.add_*` 函数，并实现 `only_contain_legal_dialects`、`write_bytecode`。 |
| `third_party/tileir/include/TritonToTileIR/Passes.td` | 用 TableGen 定义主转换 pass `convert-triton-to-cuda-tile` 的选项（approx / ftz / capability / num_ctas / num_warps / occupancy / num_stages）。 |
| `third_party/tileir/include/Transform/Passes.td` | 定义 `lift-tt-cf-to-scf`、`rewrite-assume-with-cuda-tile`、`auto-gen-memory-token` 三个预处理/后处理 pass 的语义和示例。 |
| `python/triton/compiler/compiler.py` | 上游 Triton 的 `compile()`。它调用 `add_stages` 注册阶段、按字典顺序驱动阶段循环、把每阶段产物落盘。 |
| `third_party/tileir/test/FileCheck/op-conversion.mlir` | lit 测试，其 `RUN` 行展示了一条简化的 pass-pipeline，可用来对照本讲的 pass 顺序与嵌套结构。 |

## 4. 核心概念与源码讲解

### 4.1 三阶段的注册与驱动机制

#### 4.1.1 概念说明

「三段式」并不是三个独立命令，而是**一个由上游 `compile()` 驱动的有序字典**。后端只负责把三个工厂函数塞进 `stages` 字典，上游负责按字典顺序取出并依次执行。理解这个分工，才能看懂每个阶段什么时候被调用、产物存到哪里。

关键点：Python 字典在 3.7+ 保持**插入顺序**，所以 `add_stages` 里写的是 `ttir → tileir → cubin`，执行顺序就一定是这个。

#### 4.1.2 核心流程

```
上游 compile()
  │
  │  backend.add_stages(stages, options, language)   # 后端注册三个阶段
  ├─ stages["ttir"]   = make_ttir(...)               # 工厂函数 1
  ├─ stages["tileir"] = make_tileir(...)             # 工厂函数 2
  └─ stages["cubin"]  = make_cubin(...)              # 工厂函数 3
  │
  │  for ext, compile_ir in list(stages.items())[first_stage:]:
  │      next_module = compile_ir(module, metadata)  # 依次执行
  │      落盘: file_name.<ext>                       # .ttir / .tileir / .cubin
```

每个阶段接收上一阶段的 `module` 与共享的 `metadata`，返回变换后的 `module`，传给下一阶段。产物以 `<kernel_name>.ttir`、`<kernel_name>.tileir`、`<kernel_name>.cubin` 为文件名写进缓存目录（默认 `~/.triton/cache`，可用 `TRITON_CACHE_DIR` 改）。

#### 4.1.3 源码精读

后端侧的注册逻辑——三个 lambda 分别把 `make_ttir` / `make_tileir` / `make_cubin` 绑定到阶段名上：

注册三个编译阶段，把后端方法包装成 `stages` 字典里的工厂函数，并固定执行顺序为 `ttir → tileir → cubin`（[third_party/tileir/backend/compiler.py:336-347](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L336-L347)）。

```python
def add_stages(self, stages, options, language):
    assert language == Language.TRITON, "Only TRITON language is supported for now"
    capability = int(self._parse_arch(options.arch))
    stages["ttir"] = lambda src, metadata: self.make_ttir(src, metadata, options, capability)
    stages["tileir"] = lambda src, metadata: self.make_tileir(src, metadata, options, capability)
    stages["cubin"] = lambda src, metadata: self.make_cubin(src, metadata, options, capability)
```

上游侧的驱动循环——按字典顺序取出每个阶段执行，把产物落盘，并把 `module` 滚动传给下一阶段：

上游 `compile()` 用一个 `for` 循环按 `stages` 的插入顺序依次执行每个阶段，并把产物以 `file_name.<ext>` 落盘（[python/triton/compiler/compiler.py:335-361](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L335-L361)）。

```python
for ext, compile_ir in list(stages.items())[first_stage:]:
    next_module = compile_ir(module, metadata)
    ir_filename = f"{file_name}.{ext}"
    ...
    metadata_group[ir_filename] = fn_cache_manager.put(next_module, ir_filename)
    ...
    module = next_module
```

`metadata` 在循环开始前就用 `options.__dict__` 初始化好了，所以三个阶段都能读到 `metadata["num_warps"]`、`metadata["num_ctas"]`、`metadata["num_stages"]`、`metadata["name"]` 等字段：

上游在阶段循环前用 `**options.__dict__` 初始化 `metadata`，使各阶段都能读到旋钮字段（[python/triton/compiler/compiler.py:291-296](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L291-L296)）。

```python
metadata = {
    "hash": hash,
    "target": target,
    **options.__dict__,
    **env_vars,
}
```

> 注意：正因为 `metadata` 来自 `options.__dict__`，而 `enable_approx` / `enable_ftz` 是 `@property` 不在 `__dict__` 里，所以 `make_tileir` 取这两个值时必须走 `opt.enable_approx` 而不是 `metadata["enable_approx"]`。这是 u2-l2 的「分流」在本讲的具体体现。

#### 4.1.4 代码实践

**实践目标**：确认「三个阶段确实按字典顺序、串行执行」，并能在缓存里看到三个产物文件。

**操作步骤**（源码阅读 + 可选运行）：

1. 打开 `compiler.py` 的 `add_stages`，确认 `stages` 的插入顺序是 `ttir`、`tileir`、`cubin`。
2. 打开 `python/triton/compiler/compiler.py` 的 `compile()`，找到 `for ext, compile_ir in list(stages.items())[first_stage:]:`，确认它就是按这个顺序消费 `stages` 的。
3. （可选，待本地验证）在一台已装好 TileIR 后端的机器上，设 `ENABLE_TILE=1`、`TRITON_ALWAYS_COMPILE=1`，运行一个最简单的 `@triton.jit` kernel，然后到缓存目录里找形如 `<name>.ttir`、`<name>.tileir`、`<name>.cubin` 的三个文件。

**需要观察的现象**：缓存目录里同一个 kernel hash 下应同时存在 `.ttir`、`.tileir`、`.cubin` 三类文件，对应三个阶段各自的落盘。

**预期结果**：三个文件都在，且 `.cubin` 一定是最后产出（它依赖 `.tileir` 阶段校验通过后的 module）。运行类结果标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果有人把 `add_stages` 里 `stages["tileir"]` 和 `stages["cubin"]` 的注册顺序对调，会发生什么？

**参考答案**：因为 Python 字典按插入顺序排列，对调后阶段执行顺序会变成 `ttir → cubin → tileir`。`make_cubin` 会在 `make_tileir` 之前运行，此时 IR 还是 TTIR 方言、没有 cuda_tile 模块，`write_bytecode` 会找不到 `cuda_tile::ModuleOp` 直接报错。所以**注册顺序即执行顺序**，不能随意调。

**练习 2**：为什么阶段循环里用的是 `module = next_module` 而不是原地修改？

**参考答案**：每个阶段返回变换后的新 module（MLIR pass 会改写 IR），滚动赋值让下一阶段拿到的是上一阶段的最终产物；同时落盘用的也是这个 `next_module`，保证缓存里的 IR 与实际编译一致。

---

### 4.2 make_ttir：TTIR 阶段的通用清理

#### 4.2.1 概念说明

`make_ttir` 是三段式的第一段。它的输入是前端刚生成的 TTIR（`tt.*` 方言），输出是「清理过、但仍是 TTIR 方言」的 module。它**不做方言转换**——到这一段结束时，IR 里还是 `tt.load`、`tt.dot` 这些 op。

它的职责是**通用 MLIR 清理**：内联、合并、规范化、消除公共子表达式、把循环不变量提到循环外、删除无用符号。这些 pass 和 PTX 后端的清理 pass 基本同源（见 u1-l4 的「与 PTX 后端近似」），目的是给后续的 `make_tileir` 转换阶段一个干净、规范的输入。

#### 4.2.2 核心流程

```
make_ttir(mod, metadata, opt, capability)
  ├─ metadata["name"] = mod.name        # 顺便记下 kernel 名，供后面校验用
  ├─ pm = ir.pass_manager(mod.context)  # 新建 PassManager
  ├─ pm.enable_debug()
  │   挂载顺序（均为上游 triton / 标准 MLIR pass）：
  │   1. add_inliner        内联函数体
  │   2. add_combine        TTIR 专用合并（ttir 级）
  │   3. add_canonicalizer  规范化（合并常量、简化模式）
  │   4. add_cse            公共子表达式消除
  │   5. add_licm           循环不变代码外提
  │   6. add_symbol_dce     无用符号删除
  │   （add_loop_unroll 被注释掉，未启用）
  └─ pm.run(mod, "make_ttir")           # 执行，返回仍是 TTIR
```

#### 4.2.3 源码精读

`make_ttir` 全文很短，就是「挂六个清理 pass → 运行」。注意它只读 `metadata`（记下 kernel 名），不消费 `opt` / `capability`：

`make_ttir` 挂载六个通用清理 pass（inliner / combine / canonicalizer / cse / licm / symbol_dce），其中 loop_unroll 被注释未启用（[third_party/tileir/backend/compiler.py:279-293](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L279-L293)）。

```python
@staticmethod
def make_ttir(mod, metadata, opt: TileIROptions, capability):
    # TODO: check these transform passes
    metadata["name"] = mod.name
    pm = ir.pass_manager(mod.context)
    pm.enable_debug()
    passes.common.add_inliner(pm)
    passes.ttir.add_combine(pm)
    passes.common.add_canonicalizer(pm)
    passes.common.add_cse(pm)
    passes.common.add_licm(pm)
    passes.common.add_symbol_dce(pm)
    # passes.ttir.add_loop_unroll(pm)
    pm.run(mod, "make_ttir")
    return mod
```

几个要点：

- `passes.common.*` 是上游 Triton 暴露的标准 MLIR pass（内联 / 规范化 / CSE / LICM / 符号 DCE），`passes.ttir.add_combine` 是 TTIR 专用合并。
- `metadata["name"] = mod.name` 在这里设置，后面 `make_tileir` 会用它做 kernel 名唯一性校验。
- `pm.run(mod, "make_ttir")` 的第二个参数是这次运行的标签（用于计时 / 调试输出），不是 pass。

#### 4.2.4 代码实践

**实践目标**：理解「make_ttir 不改变方言，只做清理」。

**操作步骤**：

1. 读 `make_ttir`，确认它**没有任何** `tileir.passes.add_*` 或 `add_triton_to_cudatile` 调用。
2. 在 `test/FileCheck/` 目录里没有专门测 `make_ttir` 的 lit 文件（因为这些是上游 pass），这说明 `make_ttir` 的行为基本沿用上游，TileIR 后端没有改写它。

**需要观察的现象 / 预期结果**：`make_ttir` 前后，IR 的方言都是 `tt.*`，只是 op 数量变少、结构更规整。运行验证「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `make_ttir` 里没有出现 `capability`（算力版本）的使用？

**参考答案**：`make_ttir` 只做与硬件无关的通用 MLIR 清理（内联、规范化、CSE 等），这些变换对任何 GPU 都一样。硬件相关的信息（如 `sm_100a`）要到 `make_tileir` 的转换阶段才会被烘焙进 IR。

**练习 2**：`add_loop_unroll` 被注释掉了，这会带来什么影响？

**参考答案**：循环展开在 TTIR 阶段不执行。TileIR 的循环优化（如 `loop-split`、stage 调度）由后续 `make_tileir` 和外部 `tileiras` 负责，所以这里不展开是刻意的，避免与下游优化冲突。

---

### 4.3 make_tileir：转换 pass 的挂载顺序

#### 4.3.1 概念说明

`make_tileir` 是三段式的**核心**，也是本讲的重点。它把 TTIR 方言转换成 cuda_tile 方言，是 TileIR 后端真正「下重活」的地方。它挂载的 pass 分成三类：

- **转换前的预处理**：`lift-tt-cf-to-scf`、`rewrite-assume-with-cuda-tile`。它们操作的还是 TTIR / 通用方言的 op，目的是把控制流和 `assume` 整理成下游转换更容易处理的形式。
- **主转换**：`convert-triton-to-cuda-tile`。这是真正把 `tt.*` lowering 成 `cuda_tile.*` 的 pass，所有旋钮（approx / ftz / capability / num_ctas / num_warps / occupancy / num_stages）都在这里烘焙进 IR。
- **转换后的后处理**：`auto-gen-memory-token`、inliner、`fuse-fma`、`strip-debuginfo`。它们操作的是转换后的 cuda_tile IR，负责内存排序、FMA 融合、清理调试信息。

理解「谁在转换前、谁在转换后」是本节的核心，因为它直接由「该 pass 操作哪种方言的 op」决定。

#### 4.3.2 核心流程

```
make_tileir(mod, metadata, opt, capability)
  pm = pass_manager(mod.context)
  │
  │ ── 转换前预处理（仍在 ttir / 通用方言）────────────
  ├─ 1. add_lift_tt_cf_to_scf()       cf.cond_br / cf.switch → scf（结构化控制流）
  ├─ 2. add_assume_to_tileir()        llvm.intr.assume → cuda_tile.assume（整除/对齐模式）
  │
  │ ── 主转换（ttir → cuda_tile）────────────────────
  ├─ 3. add_triton_to_cudatile(
  │        approx, ftz, capability,
  │        num_ctas, num_warps, occupancy, num_stages)
  │     ↑ 在此 pass 开头插入 cuda_tile.module 容器
  │
  │ ── 转换后后处理（已在 cuda_tile 方言）────────────
  ├─ 4. add_auto_gen_memtoken(enable_autogen_alias_mem_token)
  ├─ 5. add_inliner()                  再次内联
  ├─ 6. if opt.enable_fp_fusion:
  │        add_fma_fusion()            嵌套进 cuda_tile.module → entry
  ├─ 7. add_strip_debuginfo()          嵌套进 cuda_tile.module
  │
  ├─ pm.run(mod, "make_tileir")
  │
  ├─ only_contain_legal_dialects(mod)? ── 否 → 抛 RuntimeError（见 4.4）
  └─ kernel 名唯一性校验（见 4.3.3 末尾）
```

#### 4.3.3 源码精读

`make_tileir` 主体——注意挂载顺序、选项来源（`metadata` vs `opt`）和末尾的校验：

`make_tileir` 按顺序挂载 lift_cf → assume → 主转换 → memtoken → inliner → fma → strip，转换选项分别来自 `opt`（approx/ftz/occupancy）和 `metadata`（num_ctas/num_warps/num_stages）（[third_party/tileir/backend/compiler.py:295-330](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L295-L330)）。

```python
@staticmethod
def make_tileir(mod, metadata, opt: TileIROptions, capability):
    pm = ir.pass_manager(mod.context)
    pm.enable_debug()
    # Inherit LiftControlflowToSCF from upstream to adapt to `ControlFlow` within `triton.func`
    tileir.passes.add_lift_tt_cf_to_scf(pm)
    # The root IR for ttir is builtin moduleOp and all
    # cuda-tile ir must under tileir_moduleOp.
    # So, we will insert an tileir moduleOp directly at the beginning of TritonToCudaTile pass.
    tileir.passes.add_assume_to_tileir(pm)
    tileir.passes.add_triton_to_cudatile(
        pm,
        opt.enable_approx,
        opt.enable_ftz,
        capability,
        metadata["num_ctas"],
        metadata["num_warps"],
        opt.occupancy,
        metadata["num_stages"],
    )
    tileir.passes.add_auto_gen_memtoken(pm, opt.enable_autogen_alias_mem_token)
    passes.common.add_inliner(pm)
    if opt.enable_fp_fusion:
        tileir.passes.add_fma_fusion(pm)
    tileir.passes.add_strip_debuginfo(pm)
    pm.run(mod, "make_tileir")
    if not tileir.only_contain_legal_dialects(mod):
        raise RuntimeError(
            "Triton ttir to tileir ir failed. Some ttir ops cannot be converted to tileir."
        )

    pattern = r"entry @([a-zA-Z0-9_]*)\("
    match = re.findall(pattern, mod.__str__())
    if len(match) != 1:
        raise RuntimeError("Kernel Name matching fail")
    return mod
```

逐条拆解（每条都对应 `triton_tileir.cc` 里的一个 pybind 包装）：

**① `add_lift_tt_cf_to_scf` —— 转换前**。把 `tt.func` 内部的 `cf.cond_br` / `cf.switch` 等非结构化控制流提升成 `scf` 的结构化控制流，让下游转换能依赖 SCF（见 [include/Transform/Passes.td:50-59](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/Transform/Passes.td#L50-L59) 的描述）。

**② `add_assume_to_tileir` —— 转换前**。识别 `llvm.intr.assume` 的整除 / 对齐模式，改写成 `cuda_tile.assume`（如 `assume div_by<16 : i64>, %arg0`）；无匹配时直接删除该 assume（见 [include/Transform/Passes.td:6-48](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/Transform/Passes.td#L6-L48) 的前后 IR 示例）。这两个预处理都还在 TTIR 层。

**③ `add_triton_to_cudatile` —— 主转换**。对应 C++ 的 `createConvertTritonToCudaTilePass`，它做两件事：在 pass 开头插入一个 `cuda_tile.module` 容器（源码注释明确写了「we will insert an tileir moduleOp directly at the beginning of TritonToCudaTile pass」），然后把 `tt.*` 算子 lowering 成 `cuda_tile.*`。它的选项就是本讲的「旋钮注入点」。

选项注入的真相——`add_triton_to_cudatile` 的 7 个参数，分别来自 `opt` 和 `metadata` 两条路（[third_party/tileir/triton_tileir.cc:62-67](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L62-L67)）：

```cpp
m.def("add_triton_to_cudatile", [](mlir::PassManager &pm, bool approx,
                                    bool ftz, int capability, int num_ctas,
                                    int simt_num_warps, int occupancy, std::optional<int> num_stages) {
  pm.addPass(mlir::triton::createConvertTritonToCudaTilePass(
      approx, ftz, capability, num_ctas, simt_num_warps, occupancy, num_stages));
});
```

对应 TableGen 定义里的 7 个选项（approx-modifier / flush-to-zero-modifier / compute-capability / num-cta-in-cga / num-warps-in-cta / occupancy / num-stages），见 [include/TritonToTileIR/Passes.td:19-41](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/TritonToTileIR/Passes.td#L19-L41)。这些选项一旦烘焙进 IR，下游 `tileiras` 就不再感知 Python 旋钮了（见 u2-l7）。

**④ `add_auto_gen_memtoken` —— 转换后**。它操作的是转换后出现的 cuda_tile 内存 op（`load_ptr_tko` / `store_ptr_tko` 等），为别名访存生成串行化 token。这对应 TileIR 的无序内存模型（见 u3-l6）。它必须在转换后运行——因为转换前根本没有这些 cuda_tile op。

**⑤ `add_inliner`**：再次内联，清理转换产生的小函数。

**⑥ `add_fma_fusion`（条件）**：仅当 `opt.enable_fp_fusion` 为真时挂载。注意它**嵌套**进 `cuda_tile::ModuleOp` 再嵌套进 `cuda_tile::EntryOp`——因为 FMA 融合作用于 entry 级别的算子：

`add_fma_fusion` 把 fuse-fma 嵌套进 `cuda_tile.module → entry`，这从结构上印证了「转换后 IR 已住在 cuda_tile.module 下」（[third_party/tileir/triton_tileir.cc:68-73](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L68-L73)）。

```cpp
m.def("add_fma_fusion", [](mlir::PassManager &pm) {
  auto &mpm = pm.nest<cuda_tile::ModuleOp>();
  auto &epm = mpm.nest<cuda_tile::EntryOp>();
  epm.addPass(cuda_tile::createFuseFMAPass());
});
```

**⑦ `add_strip_debuginfo`**：同样嵌套进 `cuda_tile::ModuleOp`，剥离调试信息，减小 bytecode 体积（[third_party/tileir/triton_tileir.cc:83-87](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L83-L87)）。

**校验**：`pm.run` 之后做两件事——`only_contain_legal_dialects`（见 4.4），以及用正则 `entry @([a-zA-Z0-9_]*)\(` 检查转换后 module 里**恰好只有一个** entry kernel，否则抛 `RuntimeError("Kernel Name matching fail")`。

#### 4.3.4 代码实践

**实践目标**：对照源码写出 `make_tileir` 的完整 pass 执行顺序，并解释顺序的依据。

**操作步骤**：

1. 打开 `compiler.py` 的 `make_tileir`（L295–L330），从上到下抄下每个 `add_*` 调用，标注它操作的是「转换前（ttir）」还是「转换后（cuda_tile）」。
2. 打开 `triton_tileir.cc` 的 `init_triton_to_cudatile_passes`（L58–L101），对照每个 Python `add_*` 背后的 C++ 实现，确认 `add_fma_fusion` / `add_strip_debuginfo` 是**嵌套**进 `cuda_tile::ModuleOp` 的，而 lift_cf / assume / convert / memtoken 是挂在外层 `pm` 上的。
3. 打开一个 lit 测试的 `RUN` 行，对照「嵌套」结构：

lit 测试的 `RUN` 行展示了一条简化 pass-pipeline：`convert-triton-to-cuda-tile` 在顶层，`fuse-fma` 嵌套在 `cuda_tile.module` 之内，与本讲的嵌套结构一致（[third_party/tileir/test/FileCheck/op-conversion.mlir:1](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion.mlir#L1)）。

```
// RUN: triton-cuda-tile-opt %s ... --pass-pipeline="builtin.module(convert-triton-to-cuda-tile, cuda_tile.module(... fuse-fma ...), reconcile-unrealized-casts)"
```

**需要观察的现象 / 预期结果**：你应该得到一张「7 个 pass + 2 道校验」的顺序表（见 4.3.2 的流程图）。注意 lit 测试的 pipeline 比生产环境精简（少了 lift_cf / assume / memtoken / inline / strip 等），它是为了隔离测试单个 pass，不是完整流水线。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `lift_cf` 和 `assume` 必须在 `convert` 之前，而 `memtoken` 和 `fma_fusion` 必须在之后？

**参考答案**：由「该 pass 操作哪种方言的 op」决定。`lift_cf` 处理 `cf.*` 控制流、`assume` 处理 `llvm.intr.assume`，这些 op 在转换前的 TTIR 里才有；`convert` 一旦把它们 lowering 成 cuda_tile，原来的 `cf` / `assume` 模式就消失了，所以必须先做。反过来，`memtoken` 处理 `load_ptr_tko` / `store_ptr_tko` 等 cuda_tile 内存 op、`fma_fusion` 处理 cuda_tile entry 内的算术，这些 op 只有转换后才存在，所以必须在后。

**练习 2**：`add_triton_to_cudatile` 的 7 个参数里，哪些来自 `opt`、哪些来自 `metadata`？为什么 `enable_approx` 不能从 `metadata` 取？

**参考答案**：来自 `opt` 的有 `enable_approx`、`enable_ftz`、`occupancy`；来自 `metadata` 的有 `num_ctas`、`num_warps`、`num_stages`（外加直接传的 `capability`）。`enable_approx` / `enable_ftz` 在 `TileIROptions` 里是 `@property`，不在 `__dict__`，因此没有被展开进 `metadata`，只能通过 `opt` 访问。这也意味着改这两个环境变量会触发重编译（见 u2-l2）。

**练习 3**：`add_fma_fusion` 为什么是 `pm.nest<cuda_tile::ModuleOp>().nest<cuda_tile::EntryOp>()` 而不是直接 `pm.addPass`？

**参考答案**：转换后，所有 cuda_tile IR 都住在 `cuda_tile.module { cuda_tile.entry @kernel { ... } }` 这个嵌套结构里。MLIR 的嵌套 PassManager 要求 pass 挂到对应层级才能作用到那里的 op；FMA 融合的对象是 entry 内的算术 op，所以必须 nest 到 `EntryOp` 层。这也从侧面证明：在执行到 `add_fma_fusion` 时，主转换已经把 IR 重组进了 cuda_tile.module 容器。

---

### 4.4 合法性校验：only_contain_legal_dialects

#### 4.4.1 概念说明

转换 pass 跑完后，怎么知道「TTIR 已经被**完整** lowering 成 cuda_tile，没有遗留」？答案就是 `only_contain_legal_dialects` 这道关卡。它是一道**完整性（legalization completeness）校验**：遍历 module 里所有 op，只要发现任何一个「既不是 builtin `ModuleOp` 容器、又不属于 cuda_tile 方言」的 op，就判定转换不完整。

为什么必须做这道校验？因为 MLIR 的 conversion 框架在「某个 op 没有 lowering 模式」时，默认行为是**插入一个 `unrealized_conversion_cast` 把它留着**，而不是报错。如果没有这道关卡，残留的 `tt.*` op 会被静默带进 bytecode，最终让 `tileiras` 在很后面才崩出一个难以理解的错误。这道关卡把失败**前移**到转换刚结束的位置，给出明确的中文报错。

#### 4.4.2 核心流程

```
only_contain_legal_dialects(mod):
    result = true
    对 mod 里每个 op（walk）：
        if 这个 op 不是 ModuleOp
           and 它的方言命名空间 ≠ "cuda_tile":
            result = false          # 发现残留 op
    return result

make_tileir 里：
    if not only_contain_legal_dialects(mod):
        raise RuntimeError(
          "Triton ttir to tileir ir failed. "
          "Some ttir ops cannot be converted to tileir."
        )
```

#### 4.4.3 源码精读

C++ 侧的遍历逻辑——walk 所有 op，发现非 cuda_tile 的残留即返回 false：

`only_contain_legal_dialects` 遍历全部 op，跳过 `ModuleOp` 容器，只要出现方言命名空间不是 `cuda_tile` 的 op 就返回 false（[third_party/tileir/triton_tileir.cc:117-128](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L117-L128)）。

```cpp
m.def("only_contain_legal_dialects", [](mlir::ModuleOp mod) {
  bool only_contain_legal_dialects = true;
  mod->walk([&](mlir::Operation *op) {
    if (!llvm::isa<mlir::ModuleOp>(op) &&
        (op->getName().getDialectNamespace() !=
            mlir::cuda_tile::CudaTileDialect::getDialectNamespace())) {
      only_contain_legal_dialects = false;
    }
  });
  return only_contain_legal_dialects;
});
```

Python 侧的消费——不通过就抛出明确的 RuntimeError，拦截在 `make_tileir` 末尾：

转换后立即用 `only_contain_legal_dialects` 校验，不通过则抛出 `RuntimeError`，把失败前移到转换刚结束的位置（[third_party/tileir/backend/compiler.py:321-324](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L321-L324)）。

```python
if not tileir.only_contain_legal_dialects(mod):
    raise RuntimeError(
        "Triton ttir to tileir ir failed. Some ttir ops cannot be converted to tileir."
    )
```

注意两层含义：

- **「合法」只认 cuda_tile**：校验只放行 cuda_tile 方言和 builtin `ModuleOp` 容器。`arith`、`math`、`scf`、`cf`、`tt.*`、`LLVM` 等任何残留都会让校验失败——也就是说，主转换必须把**一切**都 lower 到 cuda_tile。
- **`ModuleOp` 被显式跳过**：外层 builtin module 容器本身不属于 cuda_tile 方言，但它是合法的结构容器，所以用 `isa<ModuleOp>` 排除。

#### 4.4.4 代码实践

**实践目标**：理解这道关卡拦的是什么，以及它和 u1-l1 提到的「尚未支持的算子」之间的关系。

**操作步骤**：

1. 读 `triton_tileir.cc` 的 `only_contain_legal_dialects`，确认它的判定条件是「非 ModuleOp 且非 cuda_tile」。
2. 回想 u1-l1 列出的「尚未支持的算子」（如 `tt.gather`、`cf.cond_br`、`math.erf`）。推理：如果一个 kernel 用了这些 op，主转换没有对应的 lowering 模式，会留下 `unrealized_conversion_cast` + 原始 op，于是这道关卡会失败并抛出上面的 `RuntimeError`。
3. （可选）在 `test/FileCheck/op-conversion-xfailure.mlir` 里观察「期望失败」的测试如何用 `-verify-diagnostics` 标注（该文件目前主要是 `RUN` 行骨架，具体内容「待确认」）。

**需要观察的现象 / 预期结果**：使用了未支持算子的 kernel，会在 JIT 编译期（不是运行期）抛出 `RuntimeError: Triton ttir to tileir ir failed. Some ttir ops cannot be converted to tileir.`，而不会带着坏 IR 继续往后走到 `tileiras`。运行类结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么是「跳过 `ModuleOp`」而不是「跳过 builtin 方言」？

**参考答案**：因为转换后的合法结构是 `builtin.module { cuda_tile.module { cuda_tile.entry {...} } }`，外层只有一个 builtin `ModuleOp` 容器。校验只需放过这一个容器 op；如果改成「放过整个 builtin 方言」，万一残留了其他不该存在的 builtin op 就漏检了。逐 op 精确跳过 `ModuleOp` 更安全。

**练习 2**：如果没有这道关卡，一个含未支持算子的 kernel 会怎样失败？

**参考答案**：MLIR 会给没被 lower 的 op 插入 `unrealized_conversion_cast`，残留的 `tt.*` op 会被带进 `write_bytecode`。`write_bytecode` 只在 `cuda_tile::ModuleOp` 上工作，要么写不出合法 bytecode，要么交给 `tileiras` 时在很后面崩出一个晦涩的错误（如不认识的 op / bytecode 校验失败）。这道关卡把失败点前移、给出明确报错，极大降低了排错成本。

---

### 4.5 make_cubin：交给外部 tileiras（承接 u2-l7）

`make_cubin` 是三段式的最后一段，它本身只有一行——直接委托给 `call_tileiras`：

`make_cubin` 只是把工作转交给 `call_tileiras`（[third_party/tileir/backend/compiler.py:332-334](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L332-L334)）。

```python
@staticmethod
def make_cubin(mod, metadata, opt: TileIROptions, capability):
    return TileIRBackend.call_tileiras(mod, metadata, opt, capability)
```

它的职责是：把校验通过的 cuda_tile module 序列化成 bytecode（用 `tileir.write_bytecode`），作为子进程调用外部 `tileiras`，产出 `.cubin`。注意 `write_bytecode` 会在 module 里**查找那个嵌套的 `cuda_tile::ModuleOp`** 来序列化——这正是 4.3 里「主转换插入了 cuda_tile.module 容器」的下游印证：

`write_bytecode` 在外层 module 里查找嵌套的 `cuda_tile::ModuleOp` 并序列化它，找不到则报错（[third_party/tileir/triton_tileir.cc:129-149](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L129-L149)）。

`tileiras` 的资源超限（共享内存 / TMEM）会被 `call_tileiras` 归类成 `OutOfResources`（供 autotuner 剪枝），其余编译失败归为 `TileirasError`。`make_cubin` 的完整细节（CUDA_HOME 注入、错误正则、路径三级解析）是 **u2-l7** 的主题，本讲只需记住：它是三段式的终点，接收的是合法的 cuda_tile bytecode。

## 5. 综合实践

**任务**：为 TileIR 后端画出一张「从 TTIR 到 cubin」的完整 pass 流水线图，并标注每个 pass 的「输入方言 → 输出方言」与失败点。

要求：

1. 从 `compiler.py` 的 `add_stages` 出发，画出三段式骨架（`make_ttir → make_tileir → make_cubin`），标注每段的输入 IR 方言和落盘文件名（`.ttir` / `.tileir` / `.cubin`）。
2. 在 `make_tileir` 框内，按顺序列出 7 个 pass（lift_cf / assume / convert / memtoken / inliner / fma / strip），用竖线标出「转换前 / 主转换 / 转换后」三个区段。
3. 在 `convert` 那一步标注 7 个注入选项，用不同颜色区分来自 `opt`（approx / ftz / occupancy）和来自 `metadata`（num_ctas / num_warps / num_stages）的参数。
4. 在 `make_tileir` 末尾画出两道关卡（`only_contain_legal_dialects`、kernel 名唯一性），标注不通过时各自抛出的 `RuntimeError` 原文。
5. 在 `make_cubin` 框里标注它会调用 `write_bytecode` 找到嵌套的 `cuda_tile::ModuleOp`，然后交给外部 `tileiras`。

**自检**：画完后，对照本讲 4.3.2 的流程图和源码逐条核对，确保顺序、来源、错误信息三项都与源码一致。如果有 GPU 环境，可用一个含 `tt.dot` 的 kernel 触发编译，到缓存目录确认三个产物文件存在（运行类「待本地验证」）。

## 6. 本讲小结

- TileIR 后端是「三段式」：`make_ttir`（TTIR 通用清理）→ `make_tileir`（TTIR→cuda_tile 转换 + 校验）→ `make_cubin`（外部 tileiras 生成 cubin），由上游 `compile()` 按字典顺序驱动，每段产物独立落盘。
- `make_ttir` 不做方言转换，只挂 6 个通用 MLIR 清理 pass（inliner / combine / canonicalizer / cse / licm / symbol_dce），与硬件无关。
- `make_tileir` 的 pass 顺序由「操作哪种方言」决定：lift_cf 和 assume 在转换前（处理 ttir / cf / llvm op），convert 是主转换，memtoken / inliner / fma / strip 在转换后（处理 cuda_tile op）。
- 旋钮在 `add_triton_to_cudatile` 注入：`enable_approx` / `enable_ftz` / `occupancy` 走 `opt`，`num_ctas` / `num_warps` / `num_stages` 走 `metadata`——根源是 approx / ftz 是 `@property` 不在 `__dict__`。
- fma_fusion 和 strip_debuginfo 必须**嵌套**进 `cuda_tile::ModuleOp`（及 `EntryOp`），因为转换后 IR 已住在该容器下；这也印证了主转换会插入 cuda_tile.module。
- `only_contain_legal_dialects` 是转换完整性的守门员：转换后只允许 cuda_tile 方言和 builtin ModuleOp 容器，任何残留 op 都会触发 `RuntimeError("... Some ttir ops cannot be converted to tileir.")`，把失败前移。

## 7. 下一步学习建议

- 本讲把 `make_tileir` 的 pass 顺序讲清了，但每个 pass 的**内部实现**没有展开。建议接着读 u3 系列逐个深入：u3-l1（C++ 插件入口与转换骨架）、u3-l2（核心 convert pass 的 lowering 模式）、u3-l3（map_elementwise 预处理）、u3-l4（lift_cf 细节）、u3-l5（assume 重写）、u3-l6（memtoken 与无序内存模型）、u3-l7（fma/loop-split/bytecode 收尾）。
- 想了解 `make_cubin` 内部如何构造 tileiras 命令、注入 CUDA_HOME、分类资源超限错误，直接进入 u2-l7。
- 想亲手用 `triton-cuda-tile-opt` 工具复现本讲的 pass-pipeline、跑 lit/FileCheck 测试，进入 u4-l1。
