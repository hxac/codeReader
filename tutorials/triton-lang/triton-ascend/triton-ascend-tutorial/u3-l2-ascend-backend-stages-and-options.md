# AscendBackend：阶段注册与 NPUOptions

> 本讲属于第 3 单元「Triton 编译流水线总览」，承接 [u3-l1](u3-l1-jit-and-compile-entry.md)。
> 在 u3-l1 里我们看到：`@triton.jit` 的 kernel 在首次调用时，会经 `JITFunction.run` 触发 `triton.compiler.compile`，由它根据 `GPUTarget` 选出唯一的 `AscendBackend`。
> 本讲要回答的问题是：**`AscendBackend` 拿到编译任务后，到底注册了一条怎样的「阶段流水线」，又是用什么开关来控制它分流的？运行期补丁 `_apply_ascend_patch()` 又是在哪一步挂上去的？**

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 `AscendBackend` 作为「编译后端门面」实现了 `BaseBackend` 的哪些契约方法，各自负责什么。
2. 读懂 `add_stages` 这一个方法——它是整条 Ascend 编译流水线的「总装配线」，能复述它注册了哪些阶段（`ttir` / `ttadapter` / `mlirbc` / `bcmlir` / `npubin`）、以什么顺序串联、在什么条件下分流。
3. 理解 `force_simt_only` 分支为何能「跳过 Linalg 直达二进制」，它与默认路径在阶段序列上的本质差异。
4. 认识 `NPUOptions` 这份「编译选项总表」的关键字段：`compile_mode`、`num_warps`、`use_bytecode`，以及 `__post_init__` 如何在**最先**调用 `_apply_ascend_patch()` 应用运行期补丁、再用一个 `compile_mode` 派生出一组联动字段。
5. 理解 `_apply_ascend_patch()` 这套运行期 monkey-patch（注入 `hacc.target`、扩展 `compiler.parse` 识别 `ttadapter`/`mlirbc`/`bcmlir`/`npubin`、给 `tl.dot` 加 HF32 守卫）为何要放在 `__post_init__` 最前面，与 `force_simt_only` 分支的「时机」有何不同。
6. 能够在源码层面追踪一种典型配置下 kernel 经历的阶段顺序。

---

## 2. 前置知识

本讲默认你已经建立以下认知（来自 u1、u2、u3-l1）：

- **编译主链路**：`TTIR → Linalg IR → AscendNPU IR → triton_xxx_kernel.o`，越往右越贴近硬件（见 u1-l1）。
- **`@triton.jit` 到 TTIR**：装饰器不立即编译，首次调用 `kernel[grid](...)` 时才触发 `triton.compiler.compile`（见 u3-l1）。
- **后端选择**：core 的 `make_backend` 依据 `GPUTarget`（含 `backend`/`arch`/`warp_size`）选出唯一后端，Ascend 后端靠 `backend == "npu"` 命中（见 u3-l1）。
- **去侵入化与补丁机制**：上游 Triton 源文件保持干净原貌，Ascend 亲和改动分两套补丁交付——构建期补丁由 `setup.py` 的 `apply_triton_ascend_patch()` 在 cmake 前贴上；运行期补丁由 `_apply_ascend_patch()` 以 monkey-patch 方式注入（见 u1-l2）。

本讲还需要两个新概念，先通俗解释：

- **阶段（stage）**：编译流水线上的一个「加工工序」。输入是一段 IR（中间表示），输出是下一段 IR（或最终二进制）。每个阶段在代码里就是一个函数：`(src, metadata) -> str | bytes`。
- **MLIR pass manager**：MLIR（多层中间表示）框架里用来「按顺序跑一组变换（pass）」的执行器。一个阶段函数内部，通常就是构造一个 pass manager、往里塞若干 pass、然后 `pm.run(mod)`。

> 一个心智模型：core 的 `compile` 是「工厂调度员」，`AscendBackend` 是「车间主任」，`add_stages` 是车间主任交给调度员的「工艺路线单」，`NPUOptions` 是附在路线单上的「加工参数表」。而 `_apply_ascend_patch()` 是在开工前先把车间里的几台关键设备（代码生成器、IR 解析器、`tl.dot` 语义）「改装」成昇腾专用版本——它必须在第一道工序之前完成。调度员照单按序执行即可。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| `third_party/ascend/backend/compiler.py` | Ascend 编译后端的「门面 + 全部阶段函数 + 选项表」 | `AscendBackend` 类、`add_stages`、`NPUOptions`（含 `__post_init__` 里最先调用的 `_apply_ascend_patch`）、各 `make_ttir`/`ttir_to_linalg`/`ttir_to_npubin` 等阶段函数；文件顶部新增的 `buffer_ir`、`ascend.ir` 导入 |
| `third_party/ascend/backend/__init__.py` | 运行期补丁的定义处 | `_apply_ascend_patch()`：三段幂等 monkey-patch（`CodeGenerator`/`compiler.parse`/`TritonSemantic.dot`） |
| `python/triton/backends/compiler.py` | core 定义的 `BaseBackend` 抽象基类与 `GPUTarget` | 后端契约（`add_stages`/`parse_options`/`supports_target` 的抽象定义） |
| `python/triton/compiler/compiler.py` | core 的 `compile` 总调度 | 它如何调用 `add_stages`、如何按顺序跑阶段 |
| `third_party/ascend/backend/utils.py` | 辅助函数 | `is_compile_on_910_95()`：决定 `npubin` 阶段走 950 还是 A2/A3 编译分支 |
| `docs/en/architecture_design_and_core_features.md` | 官方架构文档 | `compile_mode` 三种模式的官方定义表与流程图 |

---

## 4. 核心概念与源码讲解

### 4.1 AscendBackend：编译后端的「门面」

#### 4.1.1 概念说明

Triton core 对「后端」的抽象是 `BaseBackend`——一个抽象基类，规定了任何硬件后端都必须实现的几个方法。`AscendBackend` 继承它，是 Ascend NPU 对外暴露的「编译入口门面」。core 的 `compile` 只认 `BaseBackend` 这套接口，不关心具体硬件；`AscendBackend` 把这些接口「填上昇腾专属实现」，于是同一套 core 编译调度能驱动 NPU。

它的职责可以浓缩为四件事：

1. **自报家门**：告诉 core「我负责 `npu` 这个 target」。
2. **解释编译选项**：把调用方传来的选项字典，翻译成结构化的 `NPUOptions`（而 `NPUOptions` 构造时就会顺带把运行期补丁打上）。
3. **装配流水线**：告诉 core「这条流水线有哪些阶段、按什么顺序跑」。
4. **加载方言、提供 codegen 钩子、打包元数据**：把 Ascend 专属的 MLIR 方言、类型转换、运行期元数据接进来。

#### 4.1.2 核心流程

`AscendBackend` 生命周期内被 core 调用的顺序大致是：

```
make_backend(GPUTarget)            # core 按 target 选出 AscendBackend
  └─ AscendBackend.__init__(target)    # 设置二进制扩展名等
       └─ parse_options(opts_dict)      # 字典 → NPUOptions
            └─ NPUOptions.__post_init__()
                 ├─ _apply_ascend_patch()   # ★最先：打运行期补丁（本讲新增重点）
                 └─ 由 compile_mode 派生 force_simt_only 等字段（含若干 lazy 初始化）
            └─ load_dialects(ctx)        # 载入 buffer_ir / ascend.ir / ascend 方言
            └─ get_codegen_implementation(options)  # 提供 min_dot_size 等钩子
            └─ add_stages(stages, options, language) # ★装配阶段流水线（本讲核心）
            └─ pack_metadata(metadata)   # 编译完成后打包给 launcher 的元数据
```

core 拿到 `stages` 字典后，会按插入顺序逐个执行（详见 4.2），最后得到可被 launcher 加载的二进制。

> 注意时序：`_apply_ascend_patch()` 现在发生在 `NPUOptions.__post_init__` 里（即 `parse_options` 阶段），早于 `load_dialects`、`add_stages`。它把 `compiler.parse` 扩展成能识别 `ttadapter`/`mlirbc`/`bcmlir`/`npubin` 这些 Ascend 阶段扩展名——而它们正是 `add_stages` 即将注册的阶段名。这套「先改装解析器、再注册阶段」的先后顺序是刻意安排的（详见 4.3）。

#### 4.1.3 源码精读

先看文件顶部新增的导入，它把两组 Ascend 专属 IR 模块提到模块级：

[third_party/ascend/backend/compiler.py:36-37](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L36-L37) —— 相较旧版，这里新增了 `buffer_ir`（从 `triton._C.libtriton`）与 `ascend.ir`（别名 `ascend_ir`，从 `triton._C.libtriton.ascend`）两个模块级导入。它们供模块内的辅助函数（如 `_get_then_remove_rc` 调用 `ascend.ir.get_int_attr`）以及 `load_dialects` 使用，是「去侵入化」后 Ascend 方言以独立模块形态暴露的体现。

再看 core 对后端契约的定义。`BaseBackend` 把 `supports_target`、`parse_options`、`add_stages`、`load_dialects`、`get_module_map` 都声明为抽象方法：

[python/triton/backends/compiler.py:48-58](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/python/triton/backends/compiler.py#L48-L58) —— core 对 `add_stages` 的契约说明：往 `stages` 字典里填入「`ir_name => 函数(src, metadata) -> str|bytes`」的条目，阶段按插入顺序依次执行，**除最后一个阶段返回 `bytes`（二进制）外，其余阶段都返回 `str`（IR 文本）**。这条契约是理解 4.2 中「每个阶段函数返回什么」的关键。

再看 `AscendBackend` 如何兑现契约。首先是「自报家门」与初始化：

[third_party/ascend/backend/compiler.py:1205-1216](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1205-L1216) —— `supports_target` 只在 `target.backend == "npu"` 时返回 `True`（这就是 u3-l1 中 `make_backend` 能唯一选中它的原因）；`__init__` 把 `binary_ext` 设为 `"npubin"`，并把 `binary_extensions` 设为 `{"npubin", "mlirbc"}`（后者用于字节码模式，见 4.3）。注意 `__init__` 里到处是 `if self.target.backend == "npu":`，否则抛 `NotImplementedError`——这是在「自我保护」，因为这个类只服务 NPU。

然后是「解释编译选项」的 `parse_options`：

[third_party/ascend/backend/compiler.py:1218-1237](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1218-L1237) —— 它做三件事：

1. **白名单过滤**：只从 `opts` 字典里取那些确实是 `NPUOptions` 数据类字段的键（`NPUOptions.__dataclass_fields__.keys()`），多传的、拼错的键会被忽略；再用 `setdefault("arch", self.target.arch)` 兜底 `arch`。这样未知选项不会让 `NPUOptions(**args)` 报错。
2. **构造 `NPUOptions`**，并做两处 **lazy 初始化**：若调用方没给 `compile_on_910_95`，就用 `is_compile_on_910_95()`（探测当前 SoC 是否为 910_95/950/910_958b）补上；`enable_dynamic_cv_pipeline` 同理默认跟随 `is_compile_on_910_95()`。之所以「lazy」，是因为这些值依赖运行期硬件探测，不宜写死在数据类默认值里。（注意：构造 `NPUOptions(**args)` 时，`__post_init__` 已被触发，里面的 `_apply_ascend_patch()` 也在这一步执行完毕——见 4.3。）
3. **代价模型旁路**：若 `enable_costmodel_backend` 为真，则强制 `use_bytecode=False`，跳过 4.2 中 `mlirbc`/`bcmlir` 这两个 BC↔MLIR 互转阶段，让「只编译不运行」的 autotune 路径更轻量稳定。

> 这里用 `object.__setattr__(...)` 而不是直接赋值，是因为 `NPUOptions` 是 `@dataclass(frozen=True)`（见 4.3），冻结实例不允许常规赋值，只能绕过 `__setattr__` 来改字段。

最后顺带一提其余两个方法：`load_dialects`（[compiler.py:1265-1270](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1265-L1270)）把 `buffer_ir`、`ascend.ir`（`ascend_ir`）、`ascend` 三组 Ascend 专属 MLIR 方言载入上下文（其中 `buffer_ir`/`ascend_ir` 也已在文件顶部模块级导入，见上）；`get_codegen_implementation`（[compiler.py:1259-1263](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1259-L1263)）**现在只返回 `{"min_dot_size": ...}` 钩子**——旧版曾在这里调用 `_apply_ascend_patch()`，但该调用已上移到 `NPUOptions.__post_init__` 的最前面（见 4.3.3）。这是一次刻意的「时机提前」，确保补丁在任何编译分支之前就已生效。

#### 4.1.4 代码实践

**实践目标**：确认「`AscendBackend` 只服务 `npu`，且会被 core 唯一选中」，并定位运行期补丁的挂载点。

**操作步骤（源码阅读型）**：

1. 打开 `third_party/ascend/backend/compiler.py`，定位 `class AscendBackend(BaseBackend)`（约 1205 行）。
2. 阅读 `supports_target`，确认它只看 `target.backend`。
3. 想象调用方分别传入 `GPUTarget(backend="npu", ...)` 与 `GPUTarget(backend="cuda", ...)`，预判各自返回值。
4. 打开 `python/triton/compiler/compiler.py`，搜索 `make_backend`，确认 core 会遍历所有已注册后端、用 `supports_target` 选出唯一一个匹配者。
5. 在 `AscendBackend.parse_options` 里找到 `NPUOptions(**args)` 构造行，意识到构造即触发 `__post_init__`，进而定位到 `_apply_ascend_patch()` 的调用——确认它不再出现在 `get_codegen_implementation` 中。

**需要观察的现象**：`cuda` target 不会命中 `AscendBackend`（返回 `False`），因此同一台机器上 CUDA kernel 与 NPU kernel 会走完全不同的后端；运行期补丁只在构造 `NPUOptions` 时执行一次。

**预期结果**：`supports_target` 返回 `True` 当且仅当 `backend == "npu"`；`AscendBackend.__init__` 对非 `npu` target 直接抛 `NotImplementedError`；`_apply_ascend_patch()` 的唯一调用点位于 `NPUOptions.__post_init__`。

> 若本机未安装 torch_npu/CANN，无法真实运行，可只做源码追踪，明确写「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`parse_options` 为什么要用 `NPUOptions.__dataclass_fields__.keys()` 来过滤传入字典，而不是直接 `NPUOptions(**opts)`？

**参考答案**：直接展开会把调用方（如 autotune、inductor）传来的所有键都塞给数据类构造器，遇到 `NPUOptions` 没定义的键就会抛 `TypeError`。白名单过滤相当于「只取我认识的键」，提升了后端与上游之间的容错性与解耦。

**练习 2**：`AscendBackend.__init__` 里多处写了 `if self.target.backend == "npu": ... else: raise NotImplementedError`。既然 `supports_target` 已经保证只有 `npu` 能进来，这些判断是不是多余的？

**参考答案**：不算多余，属于防御性编程。`supports_target` 是「被 core 用来筛选」的契约，而 `__init__`/`add_stages` 里的判断是「自己再确认一次」。即便将来有别的代码路径绕过筛选直接实例化，也能立刻报错而不是静默产生错误行为。

---

### 4.2 add_stages：整条编译流水线的「总装配线」

#### 4.2.1 概念说明

如果说 `AscendBackend` 是车间主任，那 `add_stages` 就是它递给 core 调度员的「工艺路线单」。这是本讲（也是整个第 3 单元）最关键的一个方法：**Ascend 编译流水线到底有哪些阶段、按什么顺序串联、在什么条件下分流，全部由这一个方法决定。**

它的工作机制建立在一个核心事实之上（来自 core 的契约）：

- core 把 `stages` 建成一个普通 `dict`，调用 `backend.add_stages(stages, options, language)` 往里填条目。
- 每个 key 是「阶段名」（也就是文件扩展名，如 `"ttir"`、`"npubin"`），value 是一个函数 `(src, metadata) -> str | bytes`。
- **Python 3.7+ 的 `dict` 保持插入顺序**，core 会严格按这个顺序执行。
- core 还会用「输入源的扩展名」决定从哪个阶段开始跑（这样可以拿一段现成 IR 喂进流水线做测试；而能正确解析这些 Ascend 扩展名，正是 4.3 里 `_apply_ascend_patch()` 改造 `compiler.parse` 的功劳）。

因此，`add_stages` 写成什么顺序，流水线就跑成什么顺序；它在哪里 `return`，流水线就在哪里被截断。

#### 4.2.2 核心流程

`add_stages` 内部是一个清晰的条件树。先把它的决策逻辑画出来：

```
add_stages(stages, options, language):
  注册 stages["ttir"] = make_ttir            # 必经的通用 TTIR 优化
  ├─ if options.force_simt_only:             # 纯 SIMT 模式
  │     注册 stages["npubin"] = ttir_to_npubin
  │     return                               # ★直接返回，跳过 Linalg 及其后一切
  │
  └─ 否则（默认 / simd / 混合模式）:
        注册 stages["ttadapter"] = ttir_to_linalg   # TTIR → Linalg（u4 主线）
        ├─ if options.use_bytecode:                  # 字节码模式（默认开）
        │     注册 stages["mlirbc"] = linalg_to_bc_by_triton_mlir_opt   # Linalg → BC
        │     注册 stages["bcmlir"] = bc_to_linalg_by_bishengir_opt     # BC → MLIR 文本
        └─ 注册 stages["npubin"] = ...              # 最终二进制阶段
              ├─ if options.compile_on_910_95: linalg_to_bin_enable_npu_compile_910_95
              └─ else:                  linalg_to_bin_enable_npu_compile_A2_A3
```

于是不同配置下的**阶段序列**是：

| 配置 | 阶段序列（按执行顺序） |
|---|---|
| 默认（`unstructured_in_simt` + `use_bytecode=True`，950 机型） | `ttir → ttadapter → mlirbc → bcmlir → npubin` |
| 默认但非 950（A2/A3） | `ttir → ttadapter → mlirbc → bcmlir → npubin`（只是 `npubin` 内部换函数） |
| 关闭字节码（`use_bytecode=False`） | `ttir → ttadapter → npubin`（跳过 `mlirbc`/`bcmlir`） |
| 纯 SIMT（`force_simt_only=True`） | `ttir → npubin`（**完全跳过 Linalg/BC**） |

`npubin` 永远是最后阶段，它的函数返回 `bytes`（二进制），其余阶段返回 `str`（IR 文本）——正好符合 core「除最后阶段返回 `bytes`，其余返回 `str`」的契约。

#### 4.2.3 源码精读

先看 core 如何使用 `add_stages` 的产物，理解「插入顺序即执行顺序」：

[python/triton/compiler/compiler.py:287-324](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/python/triton/compiler/compiler.py#L287-L324) —— 这段是整条流水线的「发动机」。`stages = dict()` 建空字典；`backend.add_stages(stages, options, src.language)` 填充它；`first_stage = list(stages.keys()).index(src.ext)` 用输入扩展名定位起点（这样拿一段 `.ttadapter.mlir` 也能直接从 `ttadapter` 开始跑）；最后 `for ext, compile_ir in list(stages.items())[first_stage:]:` 按顺序逐个调用 `compile_ir(module, metadata)`，把上一个阶段的输出喂给下一个阶段。当输入是 Ascend 扩展名（如 `ttadapter`/`npubin`）时，能被正确解析，靠的正是 `_apply_ascend_patch()` 改造过的 `compiler.parse`。

> 补充一个易混淆点：阶段失败时报出的「阶段标签」（如 `make_ttir`、`ttir_to_linalg`）并不是 core 循环包出来的，而是**各阶段函数内部**把标签字符串传给 pass manager 的 `pm.run(mod, 'make_ttir')` / `pm.run(mod, 'ttir_to_linalg')`（见 [compiler.py:148](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L148) 与 [compiler.py:256](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L256)）。MLIR 的 pass manager 在某个 pass 抛错时，会用这个标签帮你定位是哪一道工序出了问题。

再看 `add_stages` 本体：

[third_party/ascend/backend/compiler.py:1272-1293](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1272-L1293) —— 这是本讲最该逐行读懂的代码。逐段拆解：

- **`stages["ttir"] = lambda src, metadata: make_ttir(src, metadata, options)`**：第一个阶段，调用 `make_ttir`。`make_ttir` 跑的是与硬件无关的通用 TTIR 优化 pass（inliner / combine / canonicalizer / cse / licm / loop-unroll 等，见 [compiler.py:134-154](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L134-L154)）。这一步所有路径都要走，所以放在最前面、在分流之前。

- **`if options.force_simt_only:` 分支**：注册 `stages["npubin"] = ttir_to_npubin` 后**立即 `return`**。这一个 `return` 就是「纯 SIMT 模式跳过 Linalg」的全部秘密——既然提前返回，`ttadapter`/`mlirbc`/`bcmlir` 根本不会被注册进字典，core 自然也就不会执行它们。`ttir_to_npubin`（[compiler.py:1139-1202](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1139-L1202)）在 `force_simt_only` 时会追加 `--enable-hivm-compile=false --enable-triton-ir-compile --pure-simt` 等选项，让 BiSheng 编译器直接吃 TTIR 文本、绕过 Linalg/AscendNPU IR 那条主线（见 [compiler.py:1152-1169](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1152-L1169)）。这与官方文档「`simt_only` 把 Triton IR 直接送给 AscendNPU IR」的描述完全吻合。

- **`stages["ttadapter"] = ... ttir_to_linalg(src, metadata, options, named_ops=True)`**：默认路径的「重头戏」，把 TTIR 经一长串 Ascend MLIR pass 变换成 Linalg IR。这条 pass 链是 u4 整个单元的主题，本讲只把它当作一个黑盒：它返回 Linalg IR 的字符串。

- **`if options.use_bytecode:` 分支**：默认 `use_bytecode=True`，于是多插两个「中转」阶段：
  - `mlirbc` = `linalg_to_bc_by_triton_mlir_opt`（[compiler.py:269-304](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L269-L304)）：用 `triton-mlir-opt --emit-bytecode` 把 Linalg 文本编译成 MLIR 字节码。
  - `bcmlir` = `bc_to_linalg_by_bishengir_opt`（[compiler.py:307-342](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L307-L342)）：再用 `bishengir-opt` 把字节码读回成 MLIR 文本（供后续 BiSheng 编译器消费）。
  
  为什么要绕「文本 → 字节码 → 文本」这一圈？因为 triton 与 BiSheng 两个工具链对 MLIR 方言的支持范围不完全一致，字节码作为双方都能稳定读写的「中间载体」，比直接传文本更健壮（详见 4.3 对 `use_bytecode` 的解释）。当 `use_bytecode=False`（如代价模型路径）时，这两个阶段不注册，`ttadapter` 的输出直接喂给 `npubin`。

- **`npubin` 的二选一**：由 `options.compile_on_910_95` 决定——950/910_95 机型走 `linalg_to_bin_enable_npu_compile_910_95`（[compiler.py:509](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L509)），A2/A3 机型走 `linalg_to_bin_enable_npu_compile_A2_A3`（[compiler.py:764](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L764)）。两者都调用 BiSheng 编译器（`_get_npucompiler_path()`），把 Linalg/MLIR 文本编成 `.o`，并返回 `bytes`。而 `compile_on_910_95` 这个开关，是在 `parse_options` 里由 `is_compile_on_910_95()` 探测出来的（见 4.1.3）。

#### 4.2.4 代码实践

**实践目标**：在 `add_stages` 中追踪「默认配置」与「`force_simt_only` 配置」两条路径，亲手验证它们的阶段序列差异。这正是本讲规格里的实践任务的前半部分。

**操作步骤（源码阅读型 + 可选运行）**：

1. 打开 `third_party/ascend/backend/compiler.py`，跳到 `def add_stages`（约 1272 行）。
2. **追踪默认配置**：假设 `options.force_simt_only == False`、`options.use_bytecode == True`、`options.compile_on_910_95 == True`（一台 950）。按插入顺序把注册进 `stages` 的 key 抄下来，应当得到 `["ttir", "ttadapter", "mlirbc", "bcmlir", "npubin"]`。逐一写出每个 key 对应的阶段函数名。
3. **追踪 `force_simt_only` 配置**：假设 `options.force_simt_only == True`。注意第 1275 行进入 `if`、第 1276 行注册 `npubin`、第 1277 行 `return`。此时 `stages` 里只有 `["ttir", "npubin"]` 两个 key——`ttadapter`/`mlirbc`/`bcmlir` 全部缺席。
4. **（可选，需 NPU 环境）运行验证**：写一个最小的 `@triton.jit` kernel（参考 u1-l4 的 vector-add），用 `kernel[grid](..., compile_mode="simt_only", num_warps=32)` 触发纯 SIMT；设置 `TRITON_DEBUG=1`，找到 dump 目录（路径会在日志里打印为 `Dumping intermediate results to ...`）。在默认模式下，dump 目录里会出现 `kernel.ttir.mlir`、`kernel.ttadapter.mlir`、`kernel.mlirbc`、`kernel.mlir`、二进制等多个文件；而在 `simt_only` 模式下，**只会看到 `kernel.ttir.mlir` 与最终二进制，没有 `ttadapter`/`mlirbc`/`mlir`**。

**需要观察的现象**：两种配置产生的「中间文件清单」不同，且差异正好对应 `add_stages` 里 `return` 的位置。

**预期结果**：
- 默认配置阶段序列：`ttir → ttadapter → mlirbc → bcmlir → npubin`。
- `force_simt_only` 配置阶段序列：`ttir → npubin`，少掉的三个阶段正是因为 `if` 分支里的 `return`。
- 若本机无 NPU/CANN，运行部分无法复现，请明确写「待本地验证」，仅完成源码追踪部分即可。

#### 4.2.5 小练习与答案

**练习 1**：假如某天你给 `add_stages` 的 `npubin` 注册行打上注释（不注册最后阶段），会发生什么？

**参考答案**：`stages` 字典里没有最后一个返回 `bytes` 的阶段，core 的循环跑完最后一个返回 `str` 的阶段（如 `bcmlir`）后就会结束，最终 `compile` 拿到的是一段 IR 文本而非二进制，launcher 无法加载，会在下游报错。这说明 `npubin` 作为「唯一的 `bytes` 产出者」是不可或缺的。

**练习 2**：为什么 `ttir` 阶段写在 `if force_simt_only` 分流**之前**，而不是放在某个分支里？

**参考答案**：因为 `make_ttir` 做的是与硬件、与编译模式都无关的通用 TTIR 优化（inliner/cse/licm 等），无论走 SIMD、混合还是纯 SIMT，这段优化都需要。把它放在分流前作为公共前置，避免在两个分支里各写一遍，是合理的复用。

**练习 3**：`use_bytecode=False` 时，`ttadapter` 阶段的输出直接被 `npubin` 消费。此时 `npubin` 函数内部如何决定输入文件名？

**参考答案**：见 [compiler.py:512](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L512)：`tmp_file_name = "kernel.mlir" if opt.use_bytecode else "kernel.ttadapter.mlir"`。`use_bytecode=False` 时写 `kernel.ttadapter.mlir`，即直接用 `ttadapter` 阶段的 Linalg 文本作为 BiSheng 编译器输入。

---

### 4.3 NPUOptions：编译选项的「总开关表」与运行期补丁

#### 4.3.1 概念说明

`NPUOptions` 是一份「编译参数总表」——一个被 `@dataclass(frozen=True)` 标记的不可变数据类，字段极多（几十个），几乎每一个都对应 BiSheng 编译器或 Ascend pass 的某个开关。本讲只挑**与流水线分流直接相关**的几样讲透：放在 `__post_init__` 最前面的运行期补丁 `_apply_ascend_patch()`、`compile_mode`、`force_simt_only`/`force_simt_template`/`parallel_mode`、`use_bytecode`、`num_warps`/`warp_size`，以及用 `compile_mode` 派生联动字段的 `__post_init__`。其余字段（各种 `enable_*`、`limit_*`）属于 BiSheng 编译优化旋钮，留待 u8、u9。

它的「不可变（frozen）」设计有两个好处：一是编译期间选项不会被意外篡改，保证同一次编译行为可预测；二是它的 `hash()` 方法把全部字段拼进缓存键，任何选项变化都会得到不同的缓存哈希，从而触发重编译。

#### 4.3.2 核心流程

`NPUOptions` 最巧妙的设计有两层。

**第一层是「最先打补丁」**：`__post_init__` 一进来，**第一件事**就是调用 `_apply_ascend_patch()`，把上游 Triton 的三处行为改造成昇腾专用版本（且带幂等保护，多次构造 `NPUOptions` 只会真正改装一次）。它必须在派生任何字段、在任何阶段函数被调用之前完成——因为其中一处补丁正是扩展 `compiler.parse`，让它认得 `ttadapter`/`mlirbc`/`bcmlir`/`npubin` 这些 Ascend 阶段扩展名（也就是 4.2 `add_stages` 即将注册的名字）。

**第二层是 `compile_mode` 这个「总开关」**：补丁打完后，用户只需指定一个三选一的 `compile_mode`，`__post_init__` 就会自动派生出 `force_simt_only`、`force_simt_template`、`parallel_mode` 这组联动字段，而这些字段正是 4.2 中 `add_stages` 用来分流的依据。也就是说：用户视角的「1 个旋钮」→ 内部展开成「几个布尔/字符串」→ 决定流水线长成什么样。

`compile_mode` 三种模式（官方文档定义）：

| `compile_mode` | 说明 | 编译路径 |
|---|---|---|
| `"simd"` | 纯 SIMD：结构化访存走 DMA，非结构化访存展开成标量循环 | `Triton IR → Linalg IR → AscendNPU IR` |
| `"unstructured_in_simt"`（**默认**） | 混合：结构化访存留 SIMD，离散访存尽量走 SIMT 模板 | `Triton IR → Linalg IR → AscendNPU IR` |
| `"simt_only"` | 纯 SIMT：Triton IR 直接送给 AscendNPU IR | `Triton IR → AscendNPU IR` |

（来源：[docs/en/architecture_design_and_core_features.md:216-220](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/docs/en/architecture_design_and_core_features.md#L216-L220)）

#### 4.3.3 源码精读

先看数据类的几个关键字段：

[third_party/ascend/backend/compiler.py:990-1002](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L990-L1002) —— `@dataclass(frozen=True)` 冻结数据类。注意几个对 NPU 而言「反直觉」的默认值：`num_warps: int = 32`、`warp_size: int = 32`、`num_stages: int = 2`、`cluster_dims: tuple = (1, 1, 1)`。在 GPU 后端里 `num_warps` 通常默认较小，而 Ascend 默认 32——这与昇腾 NPU 的核/线程模型有关（详见 u2-l2、u6）。这些名字沿用了上游 Triton 的 GPU 风格术语，但语义已被 Ascend 重新定义。

再看分流相关字段与 `compile_mode`：

[third_party/ascend/backend/compiler.py:1090-1092](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1090-L1092) —— 这里有一个**值得警惕的注释与代码不一致**：第 1090 行注释写 `# compile_mode: "simd" (default), ...`，暗示默认是 `"simd"`；但第 1092 行实际代码是 `compile_mode: str = "unstructured_in_simt"`。结合官方文档（默认确实是 `unstructured_in_simt`）可以判定：**注释已过时，应以代码与文档为准——默认是 `"unstructured_in_simt"`**。读源码时遇到这种「注释骗人」的情况，要敢于用实际默认值和文档交叉验证。紧邻其上的 `parallel_mode`（默认 `"simd"`）、`force_simt_only`（默认 `False`）、`force_simt_template`（默认 `False`）见 [compiler.py:1081-1083](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1081-L1083)。

`__post_init__` 是理解「先打补丁、再由总开关展开」的核心：

[third_party/ascend/backend/compiler.py:1113-1131](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1113-L1131) —— 逐段解读：

- **最前面的 `_apply_ascend_patch()`**（[compiler.py:1114-1116](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1114-L1116)）：从 `triton.backends.ascend` 导入并立即调用，是整个 `__post_init__` 的第一行真正逻辑。它本身定义在 [third_party/ascend/backend/__init__.py:27-113](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/__init__.py#L27-L113)，内含三段**幂等** monkey-patch（每段都有 `_ascend_*_patch_applied` 标志位防重复）：

  1. **改 `CodeGenerator.__init__`**（[\_\_init\_\_.py:30-51](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/__init__.py#L30-L51)）：在原始初始化之后，为生成的 module 注入 `#hacc.target<"arch">` 属性，让下游知道目标架构。
  2. **改 `compiler.parse`**（[\_\_init\_\_.py:57-74](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/__init__.py#L57-L74)）：保留社区对 `ttir/ttgir/llir/ptx/...` 的处理，**新增**把 `ttadapter`/`bcmlir` 当文本读、把 `mlirbc`/`npubin` 当字节读。这服务于 `ir_override`（autotune 里覆盖某阶段产物）与 `TRITON_KERNEL_OVERRIDE` 特性——这两者都依赖 `parse` 能正确读回 Ascend 阶段文件。**这正是它必须早于 `add_stages` 的根本原因**：`add_stages` 注册的扩展名，得先被 `parse` 认识。
  3. **改 `TritonSemantic.dot`**（[\_\_init\_\_.py:80-113](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/__init__.py#L80-L113)）：给 `tl.dot` 加 HF32 守卫（HF32 只对 fp32×fp32 生效，否则静默回退 `ieee`），并在用户显式设 `max_num_imprecise_acc` 时告警并忽略（昇腾不支持不精确累加）。

- **`compile_mode` 派生字段**（紧跟在补丁之后）：
  - `compile_mode == "simd"`：把 `parallel_mode` 设为 `"simd"`。（`force_simt_only`/`force_simt_template` 保持默认 `False`。）
  - `compile_mode == "unstructured_in_simt"`：把 `force_simt_template` 设为 `True`（注释说明这是历史兼容，混合模式下离散访存会尝试走 SIMT 模板）。`parallel_mode` 仍保持数据类默认值 `"simd"`。
  - `compile_mode == "simt_only"`：把 `force_simt_only` 设为 `True`，并把 `parallel_mode` 设为 `"simt"`。**正是这个 `force_simt_only = True`，在 4.2 的 `add_stages` 里触发了那条「跳过 Linalg」的短路径。**

- **末尾 `shared_mem_dynamic_size` 的设置**：`force_simt_only` 时给 `122880`，否则给 `221184`（单位字节）。这会影响纯 SIMT 路径下共享内存的动态分配上限，属于运行期资源约束。

> **时机对比（本讲规格要求理解的关键点）**：`_apply_ascend_patch()` 在 `__post_init__` 里是**无条件、最先**执行的——它对所有 `compile_mode` 一视同仁，包括 `force_simt_only`；而 `force_simt_only` 的派生发生在它**之后**的 `compile_mode` 分支里，只对 `"simt_only"` 模式为真。换言之：补丁是「与模式无关的前置改装」，`force_simt_only` 是「模式相关的分流开关」，两者一前一后、职责不同。

最后看 `use_bytecode` 与缓存哈希：

[third_party/ascend/backend/compiler.py:1095-1103](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1095-L1103) —— `use_bytecode` 默认 `True`。注释清楚地画出两条编译流：开时是 `Linalg IR → MLIR Bytecode（triton-mlir-opt）→ LLIR（bishengir-opt）→ 二进制（bishengir-compile）`，关时是 `Linalg IR → LLIR → 二进制（bishengir-compile 直连）`。这正好解释了 4.2 里 `mlirbc`/`bcmlir` 两个阶段的来由。

[third_party/ascend/backend/compiler.py:1133-1136](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1133-L1136) —— `hash()` 把所有字段（`self.__dict__.items()`）拼成键，再拼上 `get_cann_version_file_hash()`（CANN 版本），最后取 sha256。这意味着：**任意一个 `NPUOptions` 字段变化，或 CANN 版本变化，都会让缓存键不同，从而触发重新编译**。这是 Triton 编译缓存正确性的基石——也是 u3-l3「编译缓存」要展开的主题。

#### 4.3.4 代码实践

**实践目标**：亲手验证两件事——(1) `_apply_ascend_patch()` 在 `__post_init__` 中的时机与 `force_simt_only` 分支的关系；(2) `compile_mode` 如何通过 `__post_init__` 派生 `force_simt_only` 等字段，并观察这些字段对 `add_stages` 分流的影响。这是本讲规格里实践任务的后半部分。

**操作步骤（源码阅读型）**：

1. 在 `NPUOptions` 定义处确认 `force_simt_only`、`force_simt_template`、`parallel_mode`、`compile_mode` 四个字段的**默认值**（分别为 `False`、`False`、`"simd"`、`"unstructured_in_simt"`）。
2. 在 `__post_init__` 里，**首先**定位 `_apply_ascend_patch()`（第 1114-1116 行），确认它在任何 `if compile_mode` 分支**之前**、无条件执行。再去 [backend/__init__.py:57-74](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/__init__.py#L57-L74) 阅读对 `compiler.parse` 的扩展，确认它新增了 `ttadapter`/`bcmlir`/`mlirbc`/`npubin` 的识别。
3. 然后分别代入 `compile_mode` 的三个取值，推导 `force_simt_only` 最终会是 `True` 还是 `False`：
   - `"simd"` → `force_simt_only = False`
   - `"unstructured_in_simt"` → `force_simt_only = False`（只设了 `force_simt_template`）
   - `"simt_only"` → `force_simt_only = True`
4. 回到 `add_stages`（4.2），确认：只有 `force_simt_only == True` 才会走「跳过 Linalg」的短路径。于是「用户层指定 `compile_mode="simt_only"`」与「内部 `force_simt_only=True`」与「`add_stages` 提前 return」三者是一根因果链；而 `_apply_ascend_patch()` 不在这根链上——它对三种模式都同样地先执行。
5. **（可选，需 NPU 环境）**写一个 vector-add kernel，分别用 `compile_mode="simd"` 与 `compile_mode="simt_only"` 调用，对比 `TRITON_DEBUG=1` dump 出的中间文件清单，验证 `simt_only` 没有 `ttadapter`/`mlirbc`。

**需要观察的现象**：`_apply_ascend_patch()` 的执行与 `compile_mode` 无关（总是最先）；`compile_mode` 的取值，经其后的派生分支，唯一地决定了 `add_stages` 注册的阶段集合。

**预期结果**：`_apply_ascend_patch()` 总在第 1114-1116 行执行（三种模式皆然）；`compile_mode="simt_only"` ⟹ `force_simt_only=True` ⟹ `add_stages` 注册 `{ttir, npubin}` 并提前返回；其余两种模式 ⟹ `force_simt_only=False` ⟹ 注册完整 `{ttir, ttadapter, (mlirbc, bcmlir), npubin}`。

> 无 NPU 环境时，运行验证标注「待本地验证」，源码推导部分即可完成。

#### 4.3.5 小练习与答案

**练习 1**：用户在调用 `kernel[grid](..., compile_mode="simt_only")` 的同时，又显式传了 `force_simt_only=False`，会发生什么？

**参考答案**：以代码为准——`__post_init__` 在 `compile_mode == "simt_only"` 分支里**无条件**用 `object.__setattr__(self, "force_simt_only", True)` 覆盖。由于字段是 frozen，用户无法在构造后改它；而在构造时 `compile_mode` 的派生发生在 `__post_init__`，会盖掉数据类默认值。所以最终 `force_simt_only` 仍为 `True`（除非用户直接传 `force_simt_only` 进构造器且 `compile_mode` 不是 `simt_only`，存在优先级细节，实际行为「待本地验证」）。结论：`compile_mode` 是更高层的「意图声明」。

**练习 2**：为什么 `NPUOptions` 要把 CANN 版本哈希也拼进 `hash()`？

**参考答案**：因为最终二进制由 BiSheng/CANN 工具链产出，不同 CANN 版本编译出的 `.o` 不保证二进制兼容。把 CANN 版本纳入缓存键，能在升级 CANN 后自动作废旧缓存、强制重编译，避免拿到与当前运行时不匹配的旧二进制。

**练习 3**：`parse_options` 里对代价模型路径强制 `use_bytecode=False`。结合本讲的阶段序列表，说说这样省掉了哪些阶段、为什么能让 autotune 更轻量。

**参考答案**：`use_bytecode=False` 使 `add_stages` 不再注册 `mlirbc` 与 `bcmlir` 两个阶段，于是流水线从 `ttir → ttadapter → mlirbc → bcmlir → npubin` 缩短为 `ttir → ttadapter → npubin`。省掉「文本→字节码→文本」的两次外部进程调用（`triton-mlir-opt`、`bishengir-opt`），对需要在编译期快速评估大量候选配置的 costmodel autotune 而言，能显著降低单次评估开销（参见 u9-l3）。

**练习 4**：如果把 `_apply_ascend_patch()` 从 `__post_init__` 删掉、改回放在 `get_codegen_implementation`（core 在更晚才调用它），`ir_override` 覆盖一个 `ttadapter` 文件的功能可能出什么问题？

**参考答案**：core 在执行阶段循环时，遇到 `ir_override` 会调用被改造过的 `compiler.parse(ir_override, ext, context)`（见 4.2.3 的 core 引擎）。若 `_apply_ascend_patch()` 还没运行，`parse` 仍是上游原版，不认识 `ttadapter`/`bcmlir`/`mlirbc`/`npubin` 这些扩展名，会把它们当作未知分支而失败或返回空。把补丁提前到 `__post_init__`（即 `parse_options` 阶段，core 编译流程的最早处），就杜绝了这种「阶段名已注册、解析器却还不认识」的时序漏洞。

---

## 5. 综合实践

**任务**：化身「编译流水线侦探」，用本讲学到的四件套（`AscendBackend` / `add_stages` / `NPUOptions` / `_apply_ascend_patch`），完整还原三种 `compile_mode` 下 kernel 从 TTIR 到二进制的「阶段旅程」，并产出一份「阶段对照表」。

**建议步骤**：

1. 选一个你熟悉的 kernel（如 u1-l4 的 vector-add，或 u2 的迁移示例）。
2. 分别令 `compile_mode` 为 `"simd"`、`"unstructured_in_simt"`、`"simt_only"`，对每一种：
   - 写出 `NPUOptions.__post_init__` 中 `_apply_ascend_patch()` 的执行情况（三种模式都先执行一次）；
   - 写出 `__post_init__` 派生后的 `force_simt_only` / `force_simt_template` / `parallel_mode` 取值；
   - 写出 `add_stages` 注册的阶段序列（key 列表）；
   - 为每个阶段标注它的输入类型、输出类型（`str` 还是 `bytes`）、对应的阶段函数名。
3. 把三组结果汇成一张表，重点标出 `simt_only` 与另两种的差异行。
4. **进阶**：若本机有 NPU 环境，用 `TRITON_DEBUG=1` 实际 dump 三种模式的中间文件，与你推导的表逐一对照；找到 dump 日志里 `Dumping intermediate results to <dir>`，检查目录里文件名（`.ttir.mlir` / `.ttadapter.mlir` / `.mlirbc` / `.mlir` / `.o`）是否与你的阶段序列一致。
5. **反思**：用一两句话解释「为什么 `_apply_ascend_patch()` 要无条件最先执行，而 `force_simt_only` 只对 `simt_only` 模式生效」——即「前置改装」与「模式分流」的分层好处。

**预期产出**：一张「`compile_mode` × 补丁时机 × 阶段序列 × 每阶段函数」的对照表 + 一句对「先改装、再分流」分层设计的理解。无 NPU 环境时，第 4 步标注「待本地验证」。

---

## 6. 本讲小结

- `AscendBackend` 是继承 `BaseBackend` 的「编译后端门面」，靠 `supports_target`（`backend == "npu"`）被 core 唯一选中，用 `parse_options` 把选项字典翻译成 `NPUOptions` 并做硬件相关的 lazy 初始化。文件顶部新增 `buffer_ir`、`ascend.ir` 模块级导入。
- **`add_stages` 是整条 Ascend 编译流水线的「总装配线」**：它往一个 `dict` 里按顺序注册阶段（`ir_name → 阶段函数`），core 按插入顺序逐个执行，最后一个阶段返回 `bytes`、其余返回 `str`；阶段失败的标签由各阶段函数传给 `pm.run(mod, '<label>')` 提供。
- 默认（`unstructured_in_simt` + `use_bytecode=True`）阶段序列为 `ttir → ttadapter → mlirbc → bcmlir → npubin`；`use_bytecode=False` 时省去 `mlirbc`/`bcmlir`。
- **`force_simt_only` 分支会注册 `ttir → npubin` 后立即 `return`，从而跳过整个 Linalg 主线**——这是 `simt_only` 模式「Triton IR 直达二进制」的实现根源。
- `npubin` 内部再按 `compile_on_910_95`（由 `is_compile_on_910_95()` 探测）二选一：950 走 `linalg_to_bin_enable_npu_compile_910_95`，A2/A3 走 `linalg_to_bin_enable_npu_compile_A2_A3`。
- `NPUOptions` 是冻结的「选项总表」。**`__post_init__` 的第一件事是调用 `_apply_ascend_patch()`**（注入 `hacc.target`、扩展 `compiler.parse` 识别 `ttadapter`/`mlirbc`/`bcmlir`/`npubin`、给 `tl.dot` 加 HF32 守卫，三段均幂等）——它已从旧版的 `get_codegen_implementation` 上移至此，确保补丁早于任何阶段注册与执行。随后 `compile_mode` 作为用户层总开关，被派生成 `force_simt_only`/`force_simt_template`/`parallel_mode` 等内部字段；`hash()` 把全部字段 + CANN 版本拼成缓存键。注意 `compile_mode` 默认值是 `"unstructured_in_simt"`（源码注释「simd (default)」已过时，勿被误导）。

---

## 7. 下一步学习建议

- **进入 u3-l3**：本讲把 `add_stages` 当作「装配线」看，但没有展开每个阶段函数内部。u3-l3 会精读 `make_ttir` 的标准 pass、`_parse_linalg_metadata` 解析出的元数据（`kernel_name`/`tensor_kinds`/`mix_mode`），以及编译产物 `.o`/`.so` 与 Triton 缓存目录的关系。
- **进入第 4 单元**：想搞清 `ttadapter` 阶段（`ttir_to_linalg`）内部那长长一串 Ascend MLIR pass 到底做了什么，从 u4-l1「ttir_to_linalg pass 编排总览」开始，它会逐 pass 拆解这条链。
- **旁阅 u6**：本讲的 `compile_mode`/`force_simt_only` 是 SIMD/SIMT 双编译路径的入口；u6-l1 会系统讲三种模式的编译路径差异与回退语义，与本讲互为印证。
- **回看 u1-l2**：本讲的 `_apply_ascend_patch()` 正是 u1-l2 介绍的「运行期补丁」机制的具体落地，可与「构建期补丁 `apply_triton_ascend_patch()`」对照理解两套补丁的不同职责。
- **推荐继续阅读的源码**：把 `third_party/ascend/backend/compiler.py` 里 `make_ttir`、`ttir_to_linalg`（先只看它注册了哪些 `ascend.passes.ttir.*`）、`ttir_to_npubin` 三个函数对照本讲读一遍，再通读 `third_party/ascend/backend/__init__.py` 的 `_apply_ascend_patch`，建立「运行期补丁先改装、阶段函数再加工」的整体直觉。
