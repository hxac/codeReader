# make_ttir、编译产物、元数据与缓存

## 1. 本讲目标

在上一讲（u3-l2）里，我们看清了 `AscendBackend` 是怎样用 `add_stages` 把一条「阶段名 → 处理函数」的流水线登记进 Triton core 的。本讲顺着这条流水线往里走一步，回答三个具体问题：

1. **`make_ttir` 到底做了什么？** 它是 Ascend 后端注册的第一个阶段，负责对 TTIR 跑一遍「与硬件无关」的通用优化 pass。我们要逐个认清这些 pass 的作用。
2. **元数据从哪里来？** 编译走到后半段（生成 `.o` 之前），后端需要把一批「元数据」（`kernel_name`、`tensor_kinds`、`mix_mode`、`bitcodes` 等）填进 `metadata` 字典，它们决定了内核最终如何被启动。本讲要讲清两条来源：一条是用正则从 IR **文本**里抠字段（`_parse_linalg_metadata`），另一条是借助新增的 `ascend.ir` C++ 扩展模块，直接从 IR **模块对象**上读取并移除结构化属性（`_get_then_remove_rc` / `_export_coalesce_metadata` / `_adjust_metadata_by_module_result`）。
3. **编译产物和缓存长什么样？** 一次编译会落盘一堆中间文件和最终的 `.o`，并按哈希缓存。我们要理清 `TRITON_CACHE_DIR`、`TRITON_DUMP_DIR`、`TRITON_DEBUG` 之间的关系，学会在磁盘上找到自己 kernel 的所有产物。

学完后，你应当能够：看懂 `make_ttir` 的 pass 序列、说出元数据各字段的来源与用途（含两种提取机制的区别）、独立用 `TRITON_DEBUG` 把一次编译的中间产物 dump 到磁盘并逐个解释。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自 u1、u3-l1、u3-l2）：

- **TTIR**：Triton 把 Python kernel 翻译出的「与目标硬件无关」的中间表示（MLIR 方言 `tt`）。它是整条编译链的起点。
- **阶段流水线**：core 的 `triton.compiler.compile` 会按 `add_stages` 注册的顺序，依次调用每个阶段函数，把上一个阶段的产物喂给下一个阶段。默认（`use_bytecode=True`）路径为 `ttir → ttadapter → mlirbc → bcmlir → npubin`。
- **`NPUOptions`**：Ascend 后端的不可变编译选项数据类，`hash()` 方法把所有字段拼成缓存键的一部分。
- **MLIR pass / pass manager**：MLIR 用「pass 管道」对 IR 做变换，每个 pass 负责一类优化（如内联、公共子表达式消除）。

几个本讲会用到的术语：

- **pass（通道）**：对 IR 做一次特定变换的函数，如 `inliner`（内联）、`cse`（公共子表达式消除）。
- **metadata（元数据）**：编译过程中累积的一个 `dict`，沿途记录 kernel 名、张量类别、混合模式等信息，最终落盘为 `<name>.json`，供运行时启动内核使用。
- **IR 文本 vs IR 模块对象**：MLIR 既可以序列化成可读字符串（`.mlir` 文本），也可以作为内存中的 `ModuleOp` 对象被 C++ 接口操作。本讲的两种元数据提取机制正分别对应这两种形态。
- **内容寻址缓存（content-addressed cache）**：用「内容哈希」作为目录名，相同输入必然落到同一目录，从而实现命中/未命中的判断。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件，并辅以几个 core 文件理解缓存：

| 文件 | 作用 |
| --- | --- |
| `third_party/ascend/backend/compiler.py` | Ascend 后端核心：`make_ttir`、`_parse_linalg_metadata`、`_parse_ttir_metadata`、新增的 `_get_then_remove_rc`/`_export_coalesce_metadata`/`_adjust_metadata_by_module_result`、`NPUOptions.hash()`、`add_stages`、各 `linalg_to_bin_*` 阶段函数都在此。文件顶部新增 `from triton._C.libtriton import ... buffer_ir` 与 `from triton._C.libtriton.ascend import ir as ascend_ir` 两个导入，带来 `ascend.ir` / `buffer_ir` C++ 扩展模块 |
| `python/triton/compiler/compiler.py` | core 编译总调度：缓存键计算、命中判断、阶段循环、metadata 写回 |
| `python/triton/runtime/cache.py` | 缓存管理器：`FileCacheManager`、`get_dump_manager`、`get_cache_key`、`_base32` |
| `python/triton/knobs.py` | 环境变量到配置项的映射（`TRITON_CACHE_DIR`、`TRITON_DUMP_DIR`、`TRITON_DEBUG` 等） |

## 4. 核心概念与源码讲解

### 4.1 make_ttir：TTIR 的通用优化阶段

#### 4.1.1 概念说明

`make_ttir` 是 `AscendBackend.add_stages` 注册的第一个阶段（阶段名 `ttir`）。它接收 core 生成的「原始 TTIR 模块」，对其施加一批**与硬件无关的标准优化 pass**，再交由后续阶段处理。

这里有一个关键定位：`make_ttir` 里跑的 pass 全部来自 core 的 `passes.common` 与 `passes.ttir`，**不含任何 Ascend 专属 pass**。这与上一讲强调的「core / ascend 分层」一致——目标无关的优化留在通用层，Ascend 专属变换（如 `triton_to_structure`、`triton_to_linalg`）都放在下一个阶段 `ttir_to_linalg` 里。换句话说，`make_ttir` 是「公共起跑线」，所有 Triton 后端（CUDA、AMD、Ascend）在这里做几乎一样的事。

#### 4.1.2 核心流程

`make_ttir` 的执行过程可以概括为三步：

1. **（可选）补全 hash**：若 `metadata` 里还没有 `hash` 字段，则现场计算一个。实际上 core 在进入阶段前已写入 `metadata["hash"]`，所以这步通常被跳过——但它保证 `make_ttir` 也能被独立调用（例如直接喂 TTIR 文本时）。
2. **构造 pass manager 并依次添加标准 pass**：先 `pm.enable_debug()` 打开 MLIR 调试日志通道，再按固定顺序加入若干通用优化 pass，最后 `pm.run(mod, 'make_ttir')` 执行。
3. **（可选）dump**：若 `opt.debug` 为真，把优化后的 TTIR 文本写入 dump 目录。

pass 的添加顺序如下（每个 pass 的作用见源码精读）：

```
add_inliner          → 函数内联
add_combine          → Triton 特定合并（broadcast/reshape 等组合）
add_canonicalizer    → 规范化（化简到标准形式）
add_reorder_broadcast→ 重排 broadcast，便于后续向量化
add_cse              → 公共子表达式消除
add_licm             → 循环不变量外提
add_symbol_dce       → 删除无用符号定义
add_loop_unroll      → 循环展开
```

#### 4.1.3 源码精读

先看 `make_ttir` 的完整实现，尤其注意 `pm.enable_debug()` 这一行（新近加入，统一开启 MLIR 调试输出）与 pass 的添加顺序、dump 逻辑：

[third_party/ascend/backend/compiler.py:134-154](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L134-L154) —— `make_ttir`：`enable_debug()` 后对 TTIR 跑通用优化 pass；`opt.debug` 为真时把结果写入 dump 目录并打印路径。

其中几个关键 pass 的含义：

- `passes.common.add_inliner(pm)`：把被调用函数内联到调用点，减少函数调用开销、暴露更多优化机会。
- `passes.ttir.add_combine(pm)` / `passes.common.add_canonicalizer(pm)`：先做 Triton 方言特定的算子合并，再做通用规范化（如常量折叠、冗余运算消除）。
- `passes.common.add_cse(pm)`：**公共子表达式消除**（Common Subexpression Elimination），若两段运算完全相同则只算一次、复用结果。
- `passes.common.add_licm(pm)`：**循环不变量外提**（Loop-Invariant Code Motion），把循环里每次都算同样结果的运算挪到循环外。
- `passes.common.add_symbol_dce(pm)`：**死符号消除**，删掉没人引用的函数/符号定义。
- `passes.ttir.add_loop_unroll(pm)`：循环展开，把循环体复制若干份以减少分支开销。

再看 `add_stages` 里 `make_ttir` 是如何被登记为 `ttir` 阶段的（注意它和 `force_simt_only` 短路分支的关系）：

[third_party/ascend/backend/compiler.py:1272-1277](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1272-L1277) —— `stages["ttir"]` 绑定到 `make_ttir`；无论走哪条编译路径，`make_ttir` 永远是第一个阶段，`force_simt_only` 短路分支也只在它之后才 `return`。

dump 逻辑里有一段很实用：

```python
dump_manager = get_dump_manager(metadata["hash"])
print(f"Dumping intermediate results to {dump_manager.cache_dir}")
dump_manager.put(str(mod), "kernel.ttir.mlir", binary=False)
```

也就是说，只要开启调试，控制台就会直接打印出 dump 目录的绝对路径——这是本讲综合实践找文件的「入口」。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `make_ttir` 优化后的 TTIR 长什么样。

**操作步骤**：

1. 确认已按 u1-l3 安装好环境，并能跑通 `tutorials/01-vector-add.py`。
2. 在运行脚本前导出调试开关：`export TRITON_DEBUG=1`（若已安装 `torch_npu`，还需确保 `device='npu'`）。
3. 运行：`python third_party/ascend/tutorials/01-vector-add.py`。
4. 在终端输出里找到 `Dumping intermediate results to <某路径>` 这一行，复制该路径。
5. 打开该目录，找到 `kernel.ttir.mlir`，用文本编辑器查看。

**需要观察的现象**：终端会打印一条 `Dumping intermediate results to ...`；`kernel.ttir.mlir` 是优化后的 TTIR，里面能看到 `tt.func` 定义、`tt.load`/`tt.store` 等算子，且不应有明显的冗余运算（因为 cse/licm 已清理过）。

**预期结果**：能成功打开 `kernel.ttir.mlir` 并识别出 vector-add 的 kernel 结构。若设备不可用导致无法实际运行，请标注「待本地验证」，但 dump 目录路径的打印逻辑可仅通过阅读源码确认存在。

#### 4.1.5 小练习与答案

**练习 1**：`make_ttir` 里的 pass 来自 `passes.common` 和 `passes.ttir`，为什么没有 `ascend.passes.*`？

**参考答案**：因为 `make_ttir` 做的是「与硬件无关」的通用优化，属于 core 的职责；Ascend 专属变换放在下一个阶段 `ttir_to_linalg` 里。这符合「core / ascend 分层」原则——删掉这些 pass 会影响所有后端，而非只影响 Ascend。

**练习 2**：把 `add_cse` 这一行从 `make_ttir` 中删掉（仅做思想实验，不要真改源码），kernel 仍能编译通过吗？结果会一样吗？

**参考答案**：仍能编译通过，最终数值结果一致。`cse` 是优化 pass，删掉只是不再消除冗余的公共子表达式，IR 会更冗长、可能更慢，但不影响正确性。这也说明 `make_ttir` 里的 pass 都是「保语义」的优化变换。

---

### 4.2 元数据的两条来源：正则文本解析与 ascend.ir 模块属性读取

#### 4.2.1 概念说明

编译走到 `npubin` 阶段（即真正调用 BiSheng 编译器生成 `.o` 之前），后端需要把一批信息填进 `metadata` 字典。这些信息**不在 TTIR 阶段就能确定**，而是经过了 `ttir_to_linalg` 那一大串 Ascend pass 之后才「刻」进 IR 的——比如 `mix_mode`（混合模式）是由算子类型推断出来的，`bitcodes`（要链接的 bitcode）是 lower 过程中产生的。

值得特别注意的是，**填这些信息现在有两条来源、两套机制**，它们工作在不同的编译阶段、作用于 IR 的不同形态：

1. **文本正则解析（经典机制）**：`_parse_linalg_metadata(linalg: str, metadata)` 接收一段 IR **字符串**，用一组正则表达式从中抠出字段。MLIR 文本是可读字符串，正则解析简单直接，无需引入完整 IR 遍历框架。它在 `npubin` 阶段（`linalg_to_bin_*` 函数开头）被调用。
2. **C++ 模块属性读取（新机制，借助 `ascend.ir`）**：在更早的 `ttir_to_linalg`（`ttadapter`）阶段，后端持有的是内存中的 IR **模块对象**（`ModuleOp`）。此时通过新增的 `ascend.ir` 扩展模块提供的 `get_int_attr` / `remove_attr` 接口，能直接读出模块上挂的结构化整型属性（如 `hacc.coalesce_factor`、`triton_ascend.dynamic_cv_pipeline.rc`），读完即从模块上摘除。这套机制由 `_get_then_remove_rc`、`_export_coalesce_metadata`、`_adjust_metadata_by_module_result` 三个辅助函数承担。

> 名词解释：
> - **`mix_mode`（混合模式）**：标识 kernel 主要用哪类计算单元，取值如 `aiv`（纯向量 Vector）、`aic`（纯矩阵 Cube）、`mix`（Cube-Vector 融合）。它直接影响运行时分配多少物理核（详见 u2-l2）。
> - **`tensor_kind`（张量类别）**：每个张量参数的类别标记（整数），运行时据此区分输入/输出/中间张量等。
> - **`bitcode`**：编译期需要链接进内核的预编译位码文件（如 `libdevice.10.bc`，提供数学函数实现）。
> - **模块属性（module attr）**：挂在 MLIR 模块对象上的键值属性（如 `{hacc.coalesce_factor = 4}`），可被 C++ pass 写入、被 Python 侧经 `ascend.ir` 读出。

#### 4.2.2 核心流程

**机制一：`_parse_linalg_metadata(linalg, metadata)`** 的处理流程（在 `npubin` 阶段）：

1. 用一组正则在 `linalg`（IR 文本字符串）里匹配，提取下列字段并写入 `metadata`：
   - `shared`（硬编码为 `1`）：共享内存估算，目前 NPU 后端不做共享内存限制。
   - `auto_tile_and_bind_subblock`：根据是否存在 `hivm.disable_auto_tile_and_bind_subblock` 属性取反。
   - `has_auto_blockify_blacklist_op`：若 IR 里出现 `sync_block_lock`，则标记为黑名单（禁用 auto-blockify）。
   - `mix_mode`：从 `mix_mode = "..."` 提取。
   - `parallel_mode`：从 `parallel_mode = "..."` 提取。
   - `kernel_name`：从 `func.func @<名字>` 提取。
   - `name`：等于 `kernel_name`（运行时 `load_binary` 会用到）。
   - `tensor_kinds`：从所有参数的 `tt.tensor_kind = N` 提取，得到一个整数列表。
   - `required_ub_bits`：初始化为 `0`（UB 占用比特数，后续由 BiSheng 编译输出回填，供 autotune 用）。
   - `bitcodes`：从 `bitcode = "..."` 提取所有要链接的 bitcode 路径。
2. 返回 `(linalg, metadata)`，供调用方继续编译。

**机制二：`ascend.ir` 模块属性读取** 的处理流程（在 `ttir_to_linalg` 阶段、`pm.run` 之后）：

1. `_export_coalesce_metadata(mod, metadata)`：读取 `hacc.coalesce_factor`（合并因子 H）与 `hacc.coalesce_axis`（生效的 grid 轴）两个模块属性，写入 `metadata["coalesce_factor"]`/`["coalesce_axis"]`，并**从模块上移除**这两个 `hacc.*` 属性——因为下游的 hivmc 不认识未知模块属性会报错，而真正的 grid 切分已由 launcher（driver.py）接管。
2. `_adjust_metadata_by_module_result(mod, metadata, opt, ...)`：读取 `triton_ascend.dynamic_cv_pipeline.rc` 这个「结果码」。若 `rc > 0` 表示动态 CV 流水线运行失败，则把 `enable_dynamic_cv_pipeline` 回退为 `False`，并连带回退 `enable_mixed_cv` 等开关，触发后续按非 CV 路径重编。

这些字段最终都会随 `metadata` 落盘为 `<name>.json`，并在运行时被 `driver.py` 的 launcher 读取（详见 u5）。

#### 4.2.3 源码精读

先看文件顶部新增的两个导入，正是它们带来了 `ascend.ir` 与 `buffer_ir` 两个 C++ 扩展模块：

[third_party/ascend/backend/compiler.py:36-37](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L36-L37) —— 新增 `buffer_ir` 与 `from triton._C.libtriton.ascend import ir as ascend_ir`：前者加载额外的 Ascend/缓冲 IR 方言，后者暴露读写模块属性的 C++ 接口。

再看「机制二」的核心 `_get_then_remove_rc`：它通过 `ascend.ir.get_int_attr` 读属性、`ascend.ir.remove_attr` 删属性，二者皆通过 `getattr(..., None)` 防御式获取，老版本无此接口时返回 `-1`：

[third_party/ascend/backend/compiler.py:77-92](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L77-L92) —— `_get_then_remove_rc`：经 `ascend.ir` 读取并移除模块上的整型「结果码」属性，缺失接口时安全降级为 `-1`。

它被两个函数复用。其一是 `_export_coalesce_metadata`（导出 coalesce 元数据并清理 `hacc.*` 属性）：

[third_party/ascend/backend/compiler.py:95-106](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L95-L106) —— `_export_coalesce_metadata`：读取 `hacc.coalesce_factor`/`hacc.coalesce_axis` 写入 metadata，缺省分别为 1（不合并）与 -1（无轴），并把属性从模块摘掉以免污染 hivmc。

其二是 `_adjust_metadata_by_module_result`（根据动态 CV 流水线的结果码回退选项）：

[third_party/ascend/backend/compiler.py:109-119](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L109-L119) —— `_adjust_metadata_by_module_result`：读取 `triton_ascend.dynamic_cv_pipeline.rc`，非正即把 `enable_dynamic_cv_pipeline` 等选项回退，并在 debug 时打印 fallback 提示。

这两个函数都在 `ttir_to_linalg` 里、`pm.run` 执行完之后被调用，作用于内存中的 `mod` 对象：

[third_party/ascend/backend/compiler.py:257-260](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L257-L260) —— pass 跑完后先 `_adjust_metadata_by_module_result` 再 `_export_coalesce_metadata`，把模块上的结果码与 coalesce 属性「搬」进 metadata。

> 对比要点：`_export_coalesce_metadata` / `_adjust_metadata_by_module_result` 操作的是 `mod`（IR **模块对象**），在 `ttadapter` 阶段执行；而下面的 `_parse_linalg_metadata` 操作的是 `linalg`（IR **字符串**），在 `npubin` 阶段执行。两者互补，共同把 metadata 填满。

现在看「机制一」的 `_parse_linalg_metadata`。先看函数定义与正则约定（注释里给出了每条正则的匹配示例，非常有助于理解）：

[third_party/ascend/backend/compiler.py:354-387](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L354-L387) —— `_parse_linalg_metadata` 的开头：声明各正则并在注释中给出匹配示例（如 `mix_mode = "aiv"` → `aiv`、`func.func @gather_sorted_kernel` → `gather_sorted_kernel`）。

再看实际填充 `metadata` 的语句：

[third_party/ascend/backend/compiler.py:389-413](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L389-L413) —— 逐字段解析：`shared` 硬编码为 1；`mix_mode`/`parallel_mode`/`kernel_name` 用正则提取；`tensor_kinds` 转成整数列表；`bitcodes` 收集所有 bitcode 路径。

其中两条正则值得细看：

```python
# 例：%arg1: memref<?xf32> {tt.divisibility = 16 : i32, tt.tensor_kind = 0 : i32}
TENSOR_KIND_REGEX = r'%arg(\d+):[^,)]*?\{[^}]*?tt\.tensor_kind\s*=\s*([^:\s}]+)\s*:[^}]*?\}'
# 例：bitcode = "a.bc"
BITCODES_REGEX = r'bitcode\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|(\w+))'
```

`TENSOR_KIND_REGEX` 匹配每个参数声明，抓出「参数序号」和「tensor_kind 的值」；最终代码只保留 kind 值并转成 `int`，得到 `tensor_kinds` 列表。`BITCODES_REGEX` 则兼容双引号、单引号、裸标识符三种写法。

`kernel_name` 还有一道「截断」工序发生在 `AscendBackend.pack_metadata` 里——CANN 运行时限制内核名长度 ≤ 50 字符：

[third_party/ascend/backend/compiler.py:1239-1257](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1239-L1257) —— `pack_metadata`：当 `kernel_name` 超过 49 字符时截取末尾 49 个字符（CANN 运行时内核名 ≤ 50，预留结尾标记），并只打包 `kernel_name`/`hash`/`debug`/`tensor_kinds` 这四个运行时必需字段。

此外，纯 SIMT 路径（`force_simt_only`，走 `ttir_to_npubin`）用的是另一个解析函数 `_parse_ttir_metadata`，它从 TTIR（而非 Linalg）解析，并硬编码 `mix_mode = "aiv"`（因为该路径目前只支持向量内核）：

[third_party/ascend/backend/compiler.py:416-446](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L416-L446) —— `_parse_ttir_metadata`：纯 SIMT 路径的元数据解析，`mix_mode` 固定为 `"aiv"`。

#### 4.2.4 代码实践

**实践目标**：亲手从一段 IR 文本里验证正则解析的结果。

**操作步骤**：

1. 开启 `TRITON_DEBUG=1` 运行 vector-add（同 4.1.4），找到 dump 目录里的 `kernel.ttadapter.mlir`（这是 `ttir_to_linalg` 产出的 Linalg IR，也就是 `_parse_linalg_metadata` 的输入）。
2. 打开该文件，在头部查找 `func.func @<名字>`，记下 kernel 名；再查找 `mix_mode = "..."`、`tt.tensor_kind = ...`、`bitcode = ...`。
3. 把这些值与同一目录下、缓存目录（见 4.3）里的 `<name>.json` 中对应字段对照。

**需要观察的现象**：`kernel.ttadapter.mlir` 顶部有 `func.func` 定义；其 attributes 区域包含 `mix_mode`、`parallel_mode`；各参数带 `tt.tensor_kind`。`<name>.json` 里 `mix_mode`/`kernel_name`/`tensor_kinds` 的值应与正则抠出的完全一致。

**预期结果**：JSON 中的元数据字段值 = 你从 IR 文本里肉眼找到的值。若你用的是纯向量 kernel（vector-add），`mix_mode` 应为 `"aiv"`。若无法运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mix_mode` 要等到 `_parse_linalg_metadata`（npubin 阶段）才解析，而不是在 `make_ttir` 里就确定？

**参考答案**：因为 `mix_mode` 依赖 kernel 里实际出现了哪类算子（Cube/Vector），而算子到 Linalg 的 lowering 是在 `ttir_to_linalg` 阶段才完成的。`make_ttir` 阶段 TTIR 还没 lower，无法可靠判定混合模式。

**练习 2**：`_parse_linalg_metadata` 同时解析了 `kernel_name` 和 `name`，二者关系是什么？为什么 `pack_metadata` 还要对它做截断？

**参考答案**：`name = kernel_name`，二者同值；`name` 是运行时 `load_binary` 等接口使用的字段名。截断是因为 CANN 运行时要求内核名 ≤ 50 字符（考虑结尾标记，实际取 ≤ 49 字符），过长的名字（如 inductor 生成的唯一名）必须截短。

**练习 3**：`_export_coalesce_metadata` 读取完 `hacc.coalesce_factor` 后为什么还要把它从模块上「移除」？留着不行吗？

**参考答案**：不行。下游的 hivmc（BiSheng 的跨核调度 pass）会拒绝带未知模块属性的输入。真正的 grid 切分已改由 launcher（driver.py）在启动时完成，`hacc.*` 属性只是「从编译期传递到运行期的便签」，读完即应摘除，确保它们不会流进 hivmc。这正是 `ascend.ir` 提供「读 + 删」成对接口（`get_int_attr`/`remove_attr`）的原因。

---

### 4.3 编译产物与缓存机制

#### 4.3.1 概念说明

Triton 把「编译」设计成一个**内容寻址的磁盘缓存**：把所有影响编译结果的因素拼成一个键、算哈希，用哈希做目录名。下一次同样输入到来时，直接命中目录、跳过编译。这对 kernel 重复调用极其重要——否则每次都要重跑一整条 MLIR + BiSheng 流水线。

要理清三件事：

- **缓存键由什么决定？** core 的 `get_cache_key` 把「Triton 自身版本 + 源码哈希 + 后端哈希 + 选项哈希 + 影响缓存的环境变量」拼起来。
- **缓存目录在哪里？** 默认 `~/.triton/cache/`，可用 `TRITON_CACHE_DIR` 覆盖；dump 目录默认 `~/.triton/dump/`，可用 `TRITON_DUMP_DIR` 覆盖。
- **一次编译落盘了哪些文件？** 每个阶段的中间 IR、最终的 `.o` 二进制、以及一个汇总元数据的 `<name>.json`。

> 易错点区分两个调试开关：
> - `TRITON_DEBUG=1`：置位 `opt.debug`，触发**各 Ascend 阶段函数内部**的 dump（写到 `get_dump_manager(metadata["hash"])` 指向的目录），并打印调试命令行。这是本讲综合实践用的开关。
> - `TRITON_KERNEL_DUMP=1`：置位 core 的 `knobs.compilation.dump_ir`，触发 **core 阶段循环**里对每个阶段产物的 dump（写到 `get_dump_manager(src.hash())` 指向的目录）。
>
> 二者写到的子目录键不同（一个是完整缓存哈希，一个是源码哈希），所以是 dump 根目录下的**两个不同文件夹**。

#### 4.3.2 核心流程

core 的 `compile` 函数（`python/triton/compiler/compiler.py`）里，缓存相关的流程是：

1. **算缓存键**：`key = get_cache_key(src, backend, options, env_vars)`，再 `hash = sha256(key)`。
2. **建缓存管理器**：`fn_cache_manager = get_cache_manager(hash)`，目录为 `<cache_dir>/<base32(hash)>`。
3. **查命中**：若该目录下已有 `<name>.json` 且 `TRITON_ALWAYS_COMPILE` 未置位，则**缓存命中**，直接用磁盘上的产物构造 `CompiledKernel` 返回，跳过所有编译。
4. **未命中则编译**：初始化 `metadata`，按阶段循环调用各 `compile_ir`，每个阶段产物都 `fn_cache_manager.put(...)` 落盘。
5. **写回元数据**：把最终 `metadata` 序列化为 JSON 落盘，并登记到一个 group 文件里，供下次命中时定位。

缓存键的组成可以写成：

\[
\text{key} = \text{triton\_key()} \;\|\; H_{\text{src}} \;\|\; H_{\text{backend}} \;\|\; H_{\text{options}} \;\|\; \text{sorted}(\text{env\_vars})
\]

\[
\text{hash} = \text{sha256}(\text{key}), \qquad \text{目录名} = \text{base32}(\text{hash})
\]

其中 Ascend 后端的选项哈希 `H_options` 由 `NPUOptions.hash()` 给出，它把**全部选项字段**与 **CANN 版本**一起拼进去——所以只要改了任一编译选项或换了 CANN 版本，哈希就变，必然重编译。

#### 4.3.3 源码精读

先看缓存键的组装（注意它把哪些维度纳入了哈希）：

[python/triton/runtime/cache.py:307-309](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/python/triton/runtime/cache.py#L307-L309) —— `get_cache_key`：把 `triton_key()`（Triton 自身版本+各模块哈希）、源码哈希、后端哈希、选项哈希、环境变量拼成缓存键。

再看 core `compile` 里「查命中 → 命中则直接返回」的逻辑：

[python/triton/compiler/compiler.py:264-276](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/python/triton/compiler/compiler.py#L264-L276) —— 缓存命中判断：`always_compile` 未置位且 `<name>.json` 已存在，则直接从磁盘产物构造 `CompiledKernel` 返回。

以及编译完成后「写回元数据 + 登记 group」：

[python/triton/compiler/compiler.py:350-353](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/python/triton/compiler/compiler.py#L350-L353) —— 把 `metadata` 序列化为 JSON 落盘，并用 `put_group` 登记该 kernel 的所有产物文件，供下次命中定位。

Ascend 侧的选项哈希实现（注意末尾追加了 CANN 版本哈希）：

[third_party/ascend/backend/compiler.py:1133-1136](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1133-L1136) —— `NPUOptions.hash()`：把所有字段拼接后，再追加 `get_cann_version_file_hash()`，保证 CANN 工具链变化时触发重编译。

环境变量到配置项的映射（确认 `TRITON_CACHE_DIR`/`TRITON_DUMP_DIR` 的来源）：

[python/triton/knobs.py:344-346](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/python/triton/knobs.py#L344-L346) —— `dump_dir`/`override_dir`/`dir` 分别由 `TRITON_DUMP_DIR`/`TRITON_OVERRIDE_DIR`/`TRITON_CACHE_DIR` 控制，默认落在 `~/.triton/{dump,override,cache}`。

`TRITON_DEBUG` 如何变成 `opt.debug`：JIT 在准备选项时把 `knobs.runtime.debug`（即 `TRITON_DEBUG`）并进 `debug` 字段：

[python/triton/runtime/jit.py:696](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/python/triton/runtime/jit.py#L696) —— `kwargs["debug"] = kwargs.get("debug", self.debug) or knobs.runtime.debug`：`TRITON_DEBUG` 经此流入编译选项，最终成为 `NPUOptions.debug`（即各阶段函数里的 `opt.debug`）。

最后看一次编译在 dump 目录（`TRITON_DEBUG` 路径）下产出哪些 Ascend 中间文件——每个阶段函数在 `opt.debug` 为真时都会调用 `get_dump_manager(metadata["hash"]).put(...)`：

| 阶段函数 | dump 文件名 | 内容 |
| --- | --- | --- |
| `make_ttir` | `kernel.ttir.mlir` | 优化后的 TTIR |
| `ttir_to_linalg` | `kernel.ttadapter.mlir` | 经 Ascend pass 链后的 Linalg IR |
| `linalg_to_bc_by_triton_mlir_opt` | `kernel.mlirbc` | MLIR 字节码（二进制） |
| `bc_to_linalg_by_bishengir_opt` | `kernel.mlir` | 经 bishengir-opt 往返后的 MLIR 文本 |
| `linalg_to_bin_*`（npubin 阶段） | `kernel.npuir.mlir` | BiSheng 编译器的 stdout/stderr 捕获 |

而最终的 `.o` 二进制（`npubin` 阶段返回的 `bytes`）由 core 写入**缓存目录**（`<cache_dir>/<base32(hash)>/<name>.npubin`），并在 `CompiledKernel` 里作为 `self.kernel` 被运行时加载（`binary_ext = "npubin"`）。

#### 4.3.4 代码实践

**实践目标**：在磁盘上找到一次编译的全部产物，区分「缓存目录」与「dump 目录」。

**操作步骤**：

1. `export TRITON_DEBUG=1`，运行 vector-add。
2. 终端会打印 `Dumping intermediate results to <dump_path>`。`ls <dump_path>` 列出 `kernel.ttir.mlir`、`kernel.ttadapter.mlir`、`kernel.mlirbc`、`kernel.mlir`、`kernel.npuir.mlir`。
3. 再看缓存目录：默认 `ls ~/.triton/cache/`，其下每个子目录名是 `base32(hash)`。进入对应子目录（可用 `ls -lt` 找最新那个），应能看到 `<name>.ttir`、`<name>.ttadapter`、`<name>.mlirbc`、`<name>.bcmlir`、`<name>.npubin`、`<name>.json`、`__grp__<name>.json` 等文件。
4. 用 `cat <name>.json | python -m json.tool` 查看元数据，确认其中的 `kernel_name`、`tensor_kinds`、`mix_mode`、`bitcodes`、`hash` 等字段。
5. 把 `TRITON_DEBUG` 关掉再运行一次，观察终端**不再**打印 `Dumping intermediate results to ...`（因为命中缓存，连 `make_ttir` 都不会再被调用）。

**需要观察的现象**：第一次运行会打印 dump 路径并产生大量中间文件；第二次运行（同输入）不再触发编译、不再打印 dump 路径，因为命中了缓存目录里的 `<name>.json`。

**预期结果**：能分别列出 dump 目录（5 个 `kernel.*` 文件，用于阅读 IR）和缓存目录（持久产物，含 `.o` 与 `.json`）。若设备不可用，标注「待本地验证」，但目录命名与命中逻辑可通过阅读源码确认。

#### 4.3.5 小练习与答案

**练习 1**：你修改了 kernel 里一个 `BLOCK_SIZE` 常量（`constexpr`），重新运行会命中缓存吗？为什么？

**参考答案**：不会命中。`BLOCK_SIZE` 是 `constexpr`，会进入源码哈希 `H_src`（`ASTSource.hash()` 把 constants 也算进去），缓存键改变 → 新哈希 → 新目录 → 必然重编译。

**练习 2**：你想强制每次都重新编译（比如在调试 pass），应该设哪个环境变量？

**参考答案**：设 `TRITON_ALWAYS_COMPILE=1`（对应 `knobs.compilation.always_compile`），它会让 core 的命中判断短路，每次都走完整编译流程。注意它和 `TRITON_DEBUG` 不同：前者控制「是否跳过编译」，后者控制「编译时是否 dump 中间产物」。

**练习 3**：为什么 `NPUOptions.hash()` 要把 `get_cann_version_file_hash()` 也拼进去？

**参考答案**：因为最终 `.o` 是由 CANN 的 BiSheng 编译器生成的，不同 CANN 版本可能产出不同（甚至不兼容）的二进制。把 CANN 版本纳入选项哈希，能保证升级 CANN 后旧缓存自动失效、触发重编译，避免用过期的 `.o`。

## 5. 综合实践

把本讲三个模块串起来：用调试模式跑通一个 kernel，**沿着编译链逆向还原**一次编译发生了什么。

1. 准备：`export TRITON_DEBUG=1`，确保 `TRITON_CACHE_DIR` 用默认值（或自设一个空目录便于观察），删除旧缓存。
2. 运行 `third_party/ascend/tutorials/01-vector-add.py`。
3. 从终端打印的 `Dumping intermediate results to <dump_path>` 进入 dump 目录，按编译顺序整理出 5 个中间文件，并用自己的话说明每个文件处于编译链的哪一环（对应 4.3.3 的表格）。
4. 打开 `kernel.ttir.mlir`（`make_ttir` 产物）与 `kernel.ttadapter.mlir`（`ttir_to_linalg` 产物），对比二者：前者还是 `tt.` 方言，后者已经出现 `linalg.`/`memref.` 算子——直观感受「TTIR → Linalg」这一跳。
5. 进入缓存目录，打开 `<name>.json`，找出 `kernel_name`、`mix_mode`、`tensor_kinds`、`bitcodes` 四个字段，回到 `kernel.ttadapter.mlir` 里用肉眼/搜索定位它们的来源（即 `_parse_linalg_metadata` 正则匹配的位置），验证「JSON 字段 = IR 文本里的值」。
6. 写一段小结：说明 `make_ttir`（通用优化）、元数据的两条来源（正则文本解析 + `ascend.ir` 模块属性读取）、缓存机制（避免重复编译）三者如何协作，把「第一次编译慢、第二次秒开」的现象解释清楚。

> 提示：若在第 4 步发现 `kernel.mlirbc` 是二进制打不开，这是正常的——它是 MLIR 字节码，可用 `triton-mlir-opt` 等工具反序列化；本实践只需阅读文本类的 `.mlir` 文件即可。

## 6. 本讲小结

- `make_ttir` 是 Ascend 后端的第一个阶段，跑的是一批**与硬件无关**的通用优化 pass（inliner、combine、canonicalizer、cse、licm、symbol_dce、loop_unroll 等），不含任何 `ascend.passes.*`；执行前会先 `pm.enable_debug()`。
- 元数据现在有**两条来源**：① `_parse_linalg_metadata` 在 `npubin` 阶段用正则从 Linalg IR **文本**抠出 `kernel_name`/`mix_mode`/`parallel_mode`/`tensor_kinds`/`bitcodes` 等；② 借助新增的 `ascend.ir` C++ 扩展（`get_int_attr`/`remove_attr`），`_export_coalesce_metadata` 与 `_adjust_metadata_by_module_result` 在 `ttir_to_linalg` 阶段直接从 IR **模块对象**读取 `hacc.coalesce_factor`、动态 CV 流水线结果码等结构化属性，读完即从模块摘除。
- `kernel_name` 还会在 `pack_metadata` 里按 CANN 的 50 字符限制截断，且只打包运行时必需的四个字段。
- 纯 SIMT 路径走 `_parse_ttir_metadata`，从 TTIR 解析且 `mix_mode` 固定为 `"aiv"`。
- Triton 用**内容寻址缓存**避免重复编译：缓存键 = Triton 版本 + 源码哈希 + 后端哈希 + 选项哈希 + 环境变量；`NPUOptions.hash()` 额外纳入 CANN 版本，保证工具链升级后旧缓存失效。
- `TRITON_DEBUG` 触发各 Ascend 阶段函数内部的 IR dump（打印 `Dumping intermediate results to ...`），`TRITON_KERNEL_DUMP` 触发 core 阶段循环的 dump，二者落到 dump 根目录下**不同**的子目录。
- 最终 `.o` 二进制以 `<name>.npubin` 落在**缓存目录**，元数据汇总在 `<name>.json`，运行时据此加载内核。

## 7. 下一步学习建议

- **进入 pass 链细节**：本讲只到 `make_ttir` 和元数据解析。下一讲 u4-l1「ttir_to_linalg：Ascend pass 编排总览」会逐个拆解 `ttir_to_linalg` 里那一长串 `ascend.passes.ttir.*` 的调用顺序与开关条件，是理解 Ascend 编译后端的核心；其中也包括那些把 `hacc.*` 等模块属性「写」进 IR、再由本讲的 `ascend.ir` 读出的 pass。
- **理解运行时如何用元数据**：`kernel_name`/`mix_mode`/`tensor_kinds`/`coalesce_factor` 等字段如何被 launcher 读取、如何调用 `rtKernelLaunch`，见 u5（运行时驱动与 kernel 启动）。
- **动手验证缓存**：结合 u10-l3「环境变量与编译选项速查」，系统尝试 `TRITON_ALWAYS_COMPILE`、`TRITON_CACHE_DIR`、`TRITON_KERNEL_OVERRIDE` 等开关，加深对缓存与覆盖机制的理解。
- **源码延伸阅读**：`python/triton/compiler/compiler.py` 的 `compile` 与 `CompiledKernel` 是理解所有后端缓存行为的公共入口，值得通读一遍。
