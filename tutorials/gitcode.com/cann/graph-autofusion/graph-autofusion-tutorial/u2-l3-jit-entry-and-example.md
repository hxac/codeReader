# JIT 代码生成入口与首个示例

## 1. 本讲目标

上一讲（u2-l2）我们把 `super_kernel/` 组件拆成了两层：JIT 层（Python，编译期）与 AOT 层（C++，运行期），并指出两层只靠公共 C 头对接。本讲深入 **JIT 层**，回答三个问题：

1. JIT 层的入口 `compile()` 到底做了什么？它是如何把一堆「已经各自编译好的子算子」缝合成一个超核的？
2. `op_infos`（超核整体元数据）和 `sub_op_infos`（每个子算子元数据）这两类元数据各自记录了什么字段、起到什么作用？
3. SuperKernel 提供的示例长什么样？作为用户，我在哪里标定融合范围、又在哪里真正触发了 `compile()`？

学完后你应当能够：读懂 `compile()` 的三段式流程（解析元数据 → 生成 C++ 源文件 → 调用底层编译器），说清两类元数据的职责分工，并能区分「框架级 scope 用法」与「底层直接调用 compile() 用法」。

## 2. 前置知识

本讲默认你已经读过 u2-l1（SuperKernel 原理）与 u2-l2（目录结构与构建产物）。先回顾几个关键结论：

- JIT 层入口是 `compile()`，AOT 层入口是 `aclskOptimize()`；超核思想是把 N 个子算子缝合成 1 个 kernel，省下 N−1 次调度。
- 构建产物有两个：`libascendsk.so`（AOT 运行时）与 `superkernel-*.whl`（JIT Python 包）。

本讲还要补三个新概念，它们是理解 `compile()` 输入的前提：

- **子算子产物（kernel_meta / json_path / bin_path）**：每个子算子在进入超核之前，**已经被单独编译过一次**，产出两部分——一个 `.o` 设备二进制文件（`bin_path`）和一个 `.json` 元信息文件（`json_path`，记录 `blockDim`、`kernelName`、`opParaSize` 等）。`compile()` 的输入不是源码，而是这份「已编译子算子清单」。
- **kernel_type（核类型）**：子算子跑在哪类核上——AIV（向量核 Vector）、AIC（Cube 核）、或 MIX（混核）。超核要据此决定整体调度方式和同步范围。
- **stream（执行流）**：子算子可能分布在不同流上，流与流之间需要插入同步（notify/wait）。是否出现「双流」会改变代码生成路径。

> 一句话直觉：`compile()` 像一个「装配车间」——子算子是预制构件（各自已有 `.o`/`.json`），`compile()` 按图纸（`op_list` 的顺序 + 编译选项）把它们焊成一个整体，并在接缝处插入必要的「连接件」（同步、预取指令），最后把焊好的源文件送进底层编译器烧制成一个超核 `.o`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `super_kernel/src/jit/superkernel/super_kernel.py` | JIT 入口 `compile()` 与超核 C++ 源码生成器 `gen_super_kernel_file()` |
| `super_kernel/src/jit/superkernel/super_kernel_op_infos.py` | `SuperOperatorInfos`（即 op_infos）：解析 `op_list`、汇总超核类型/block_num、产出 `compile_info` |
| `super_kernel/src/jit/superkernel/super_kernel_sub_op_infos.py` | `SubOperatorInfos`（即 sub_op_infos）：单个子算子的元数据与为它生成的代码片段 |
| `super_kernel/src/jit/superkernel/super_kernel_constants.py` | 枚举常量：核类型、预取/Early-Start 等模式 |
| `super_kernel/examples/super_kernel_base/superkernel_scope.py` | 框架级（torchair）scope 示例 |
| `super_kernel/examples/super_kernel_runtime_ascendc_only/compile_sk.py` | 底层直接调用 `compile()` 的示例 |

## 4. 核心概念与源码讲解

### 4.1 compile() 代码生成入口

#### 4.1.1 概念说明

`compile()` 是 JIT 层对内的唯一总入口。它的职责可以用一句话概括：**读入一组已编译子算子的清单与选项，把它们缝合成一个超核源文件，并真正编译成 `.o`**。

它解决的问题：上一讲说过，超核要靠「编译期掌握的全部先验信息」来做缝合。这些先验信息就体现在调用 `compile()` 时传入的 `kernel_infos` 字典里——里面列出每个子算子的 `bin_path`/`json_path`、所在的流、事件依赖，以及 `super_kernel_options` 这一串编译选项（如 `early-start`、`preload-code`、`split-mode` 等）。`compile()` 拿到这些信息后，先解析成结构化元数据，再据此拼出 C++ 源码，最后调用底层编译器。

注意一个关键设计：`compile()` 不接受「源码」，只接受「已编译子算子」。也就是说，子算子的算子逻辑（它们算什么）早已固化在各自的 `.o` 里；`compile()` 关心的是**怎么把它们按顺序、按核类型、按同步约束拼到一起**。

#### 4.1.2 核心流程

`compile()` 的执行可以用下面这段伪代码概括：

```
compile(kernel_infos, called_kernel_name, compile_infos):
    1. reset 全局存储          # 每次入口必须重置，避免上次编译残留
    2. 初始化特性开关
    3. if 当前 SoC 不支持超核:  报错退出
    4. if 超核 .o 已存在:       直接返回（编译缓存命中）
    5. if op_list 为空:         报错退出
    6. super_operator = SuperOperatorInfos(kernel_infos, called_kernel_name)
                               # 解析元数据：op_infos + 每个 sub_op_infos
    7. gen_super_kernel_file(super_operator)
                               # 拼接出 auto_gen_<name>_kernel.cpp
    8. compile_super_kernel(super_operator.compile_info, log_path)
                               # 调用底层编译器，烧制成 .o
```

其中第 6、7、8 步是核心三段式：**解析元数据 → 生成源文件 → 真正编译**。第 4 步的缓存机制很重要：同一个超核若已经编译过（`.o` 存在），`compile()` 会直接返回，不重复编译——这是 SuperKernel 在整网重编译时控制编译时间的关键。

#### 4.1.3 源码精读

先看函数签名与文档，文档里直接给出了 `kernel_infos` 的结构示例（`op_list` + `super_kernel_options`）：

[super_kernel/src/jit/superkernel/super_kernel.py:1044-1055](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L1044-L1055) —— `compile()` 的入口与参数说明，`kernel_infos` 形如 `{"op_list": [...], "super_kernel_options": ...}`，`called_kernel_name` 是生成的超核名（默认 `ascendc_super_kernel_plus`）。

接下来是前置校验与缓存三连：

[super_kernel/src/jit/superkernel/super_kernel.py:1057-1068](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L1057-L1068) —— 依次完成：重置全局存储、初始化特性、SoC 支持性检查；然后是**编译缓存**——若 `kernel_meta_dir` 下已有 `called_kernel_name + ".o"` 就直接 `return`，跳过整个编译。

然后是空校验与三段式主流程：

[super_kernel/src/jit/superkernel/super_kernel.py:1070-1075](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L1070-L1075) —— 校验 `op_list` 非空；构造 `SuperOperatorInfos`（解析元数据）；调用 `gen_super_kernel_file()` 生成源文件；调用 `compile_super_kernel()` 真正编译。

再看生成源文件的总入口，它根据「是否双流」走两条不同的拼接路径：

[super_kernel/src/jit/superkernel/super_kernel.py:904-907](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L904-L907) —— `gen_super_kernel_file()`：若 `enable_double_stream` 为真，转走 `gen_2_real_stream_super_kernel_file()`（双流版本，按 aic/aiv 两类核分别生成函数体），否则走单流拼接逻辑，最终都写出 `auto_gen_<kernel_name>_kernel.cpp`。

> 小提示：`gen_super_kernel_file()` 内部会遍历每个子算子（`sub_operator`），把它们各自的 `kernel_declare`（声明）、`kernel_call_block`（调用语句块）、preload/datacache/同步代码按顺序拼进最终源文件。这些片段正是下一节 `sub_op_infos` 生成的。

#### 4.1.4 代码实践（源码阅读型）

这是一个不需要硬件、纯读源码的实践，目标是把 `compile()` 的三段式看穿。

1. **实践目标**：在不运行任何命令的前提下，能用自己的话讲清 `compile()` 从输入到 `.o` 的三步。
2. **操作步骤**：
   - 打开 `super_kernel/src/jit/superkernel/super_kernel.py`，定位 `def compile(...)`（约 1044 行）。
   - 在函数体内标注三段式的边界：哪几行是「解析元数据」、哪几行是「生成源文件」、哪几行是「真正编译」。
   - 再定位 `def gen_super_kernel_file(...)`（约 904 行），找到那个决定走「单流」还是「双流」分支的 `if`。
3. **需要观察的现象**：函数开头对 `global_var_storage` 做了什么；缓存判断依据的是哪个文件的存在性；`op_list` 为空时调用的是什么报错函数。
4. **预期结果**：你能指出「重置全局存储」是第 1057 行、缓存命中是 1067–1068 行、三段式主流程是 1072–1074 行。
5. **待本地验证**：若你想观察真实产物，需要在装好 CANN 的 NPU 环境运行 4.3 节的示例，本实践不强制。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `compile()` 一开头必须调用 `global_var_storage.global_storage_reset()`，而不是假设全局存储是干净的？
**参考答案**：因为同一个 Python 进程里可能连续编译多个超核（整网有大量超核区域），上一次编译残留在全局存储里的状态会污染下一次。每次入口显式重置，保证每次编译相互独立、结果确定。

**练习 2**：`compile()` 在什么情况下会「什么都不做就返回」？这个设计带来什么好处？
**参考答案**：当 `kernel_meta_dir` 下已经存在 `called_kernel_name + ".o"` 时直接返回（编译缓存）。好处是整网重编译时，未变化的超核不重复编译，显著缩短编译时间。

---

### 4.2 op_infos / sub_op_infos 元数据

#### 4.2.1 概念说明

上一节说 `compile()` 的第一步是「解析元数据」，这个元数据分成两类，是 `compile()` 的「图纸」：

- **op_infos（`SuperOperatorInfos`）**：描述**整个超核**——它由哪些子算子组成、整体跑在什么核类型上、用多少 block、开了哪些优化选项。它对应 `super_kernel_op_infos.py`。
- **sub_op_infos（`SubOperatorInfos`）**：描述**单个子算子**——它的 `json_path`/`bin_path`、所在流号、核类型、block 数，以及它要被生成成什么样的调用代码（`kernel_declare`、`kernel_call_block` 等）。它对应 `super_kernel_sub_op_infos.py`。

二者是**聚合关系**：`SuperOperatorInfos` 内部有一个 `info_base` 列表，列表里每个元素就是一个 `SubOperatorInfos`。可以把 op_infos 想成「一张装配图」，sub_op_infos 想成「图上每个零件的规格卡」。

#### 4.2.2 核心流程

`SuperOperatorInfos.__init__()` 的执行流程（即 `compile()` 第 6 步内部）：

```
1. 从 kernel_infos 取出 op_list、解析 super_kernel_options
2. 对 op_list 中每个 op_info：
       若含 "json_path"，构造一个 SubOperatorInfos，加入 info_base
3. init_sub_operators():
       对每个子算子调用 init_of_sub_operator_info() 与 code_gen()
       （生成该子算子的声明/调用代码片段）
4. get_summary_type_and_options():
       汇总所有子算子的核类型/block 数 → 推导超核整体 kernel_type 与 block_num
5. gen_super_kernel_params():  汇总超核的参数列表
6. gen_compile_info():         产出交给底层编译器的 compile_info 字典
```

其中第 4 步的「类型汇总」是关键：超核的整体核类型不是随便选的，而是由「子算子里最多的 AIC 数 / AIV 数」决定的。例如当 AIV 数量超过 AIC 的两倍时，会选 `KERNEL_TYPE_MIX_AIC_1_2`（1 个 Cube 核配 2 个 Vector 核）。

#### 4.2.3 源码精读

先看 op_infos 的类定义与 `op_list` 解析：

[super_kernel/src/jit/superkernel/super_kernel_op_infos.py:88-105](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_op_infos.py#L88-L105) —— `SuperOperatorInfos.__init__`：取出 `op_list`（第 91 行）、用 `parse_super_kernel_options` 解析选项（第 98 行），并初始化 `split_mode`、`profiling_mode`、`feed_sync_all_mode`、`debug_aic_num`/`debug_aiv_num` 等超核级字段。

再看「逐个构造 sub_op_infos 并加入 info_base」的循环：

[super_kernel/src/jit/superkernel/super_kernel_op_infos.py:107-111](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_op_infos.py#L107-L111) —— 遍历 `op_list`，跳过没有 `json_path` 的项，为每个有效项构造 `SubOperatorInfos(index, op_info, stream_id, self.op_options, self.compile_log_path)` 并 append 到 `self.info_base`。

然后是子算子代码片段的生成入口：

[super_kernel/src/jit/superkernel/super_kernel_op_infos.py:574-588](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_op_infos.py#L574-L588) —— `init_sub_operators()`：先对每个子算子调 `init_of_sub_operator_info()`，再按 ffts 模式计算 `param_offset`，最后调用 `sub_op.code_gen(...)` 生成该子算子的声明与调用代码（即 `kernel_declare`/`kernel_call_block`），并累加参数偏移。

接着看类型汇总逻辑：

[super_kernel/src/jit/superkernel/super_kernel_op_infos.py:759-806](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_op_infos.py#L759-L806) —— `get_summary_type_and_options()`：遍历子算子，用位标记（`final_kernel_type = ... | 0b1` 等）记录出现了哪些核类型，并跟踪 `max_aic_num`/`max_aiv_num`，最后交给 `get_finale_type_and_block_num()` 决断。

[super_kernel/src/jit/superkernel/super_kernel_op_infos.py:730-756](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_op_infos.py#L730-L756) —— `get_finale_type_and_block_num()`：根据位标记与 max 核数，把超核归类为 `MIX_AIV_1_0`/`MIX_AIC_1_0`/`MIX_AIC_1_1`/`MIX_AIC_1_2` 之一，并设定 `block_num`。

最后看产出给底层编译器的 `compile_info` 字典：

[super_kernel/src/jit/superkernel/super_kernel_op_infos.py:1032-1052](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_op_infos.py#L1032-L1052) —— `compile_info` 字典，包含 `block_num`、`kernel_type`、`sub_operator`（各子算子的 bin/kernel_names）、`kernel_file`（生成的源文件路径）、`compile_option`、`kernel_name`、`link_mode`、各 `param_offset` 与事件列表等——这正是 `compile_super_kernel()` 真正编译时读的配置。

再看 sub_op_infos 一侧记录了哪些字段：

[super_kernel/src/jit/superkernel/super_kernel_sub_op_infos.py:48-63](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_sub_op_infos.py#L48-L63) —— `SubOperatorInfos.__init__`：从 `info_dict` 取出 `json_path`、`bin_path`（第 51–52 行）、`task_type`（第 56 行，默认 `normal` 即静态算子）、`send_event_list`/`recv_event_list`（第 62–63 行，事件依赖）、`stream_index`（第 55 行，真实流号）。这些就是单个子算子的规格卡。

> 字段速查：sub_op_infos 的「身份字段」是 `json_path`/`bin_path`（它编译产物在哪）、「调度字段」是 `stream_index`/`kernel_type`/`block_num`（它怎么跑）、「依赖字段」是 `send_event_list`/`recv_event_list`（它和谁要同步）；后续的 `kernel_declare`/`kernel_call_block` 则是它「生成出来的代码」。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：亲眼看到 `compile()` 的输入 `kernel_infos` 真实长什么样，并标注出 sub_op_infos 用到的字段。
2. **操作步骤**：
   - 打开 `super_kernel/examples/super_kernel_runtime_ascendc_only/compile_sk.py`，定位函数 `compile_superkernel(...)`（约 322 行）。
   - 找到它构造的 `kernel_info` 字典与对 `super_kernel.compile(...)` 的调用。
3. **需要观察的现象**：`op_list` 里每个元素包含哪些键；`super_kernel_options` 是一个什么样的字符串。
4. **预期结果**：你会看到 `op_list` 每项形如 `{"stream_id": 1, "bin_path": ..., "json_path": ...}`，对应 sub_op_infos 的 `bin_path`/`json_path`/`stream_index`；`super_kernel_options` 形如 `"compile-options=-g:"`，正是 `parse_super_kernel_options` 解析的对象。
5. **待本地验证**：本实践为阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`op_list` 里如果有一项缺少 `json_path`，会发生什么？
**参考答案**：根据 `SuperOperatorInfos.__init__` 第 108 行的 `if "json_path" not in op_info: continue`，这一项会被**静默跳过**，不会构造 `SubOperatorInfos`，也不会进入 `info_base`。所以一个没有 `json_path` 的项等于「不参与融合」。

**练习 2**：超核的整体 `kernel_type` 是怎么定的？为什么不能让用户随便指定？
**参考答案**：由 `get_summary_type_and_options()` 汇总所有子算子的核类型与 max AIC/AIV 数量，再由 `get_finale_type_and_block_num()` 自动归类。因为整体核类型必须「装得下」所有子算子——比如里面有 Cube 算子就必须有 AIC，Vector 算子多到一定程度就得用 1:2 的混核配置。用户随意指定可能导致某些子算子无核可跑。

---

### 4.3 scope 基础示例

#### 4.3.1 概念说明

SuperKernel 给用户提供了**两个层面**的用法，对应两个示例，理解它们的区别是本节重点：

- **框架级用法（`superkernel_scope.py`）**：基于 torchair。用户在 `forward()` 里用 `with tng.scope.super_kernel("sk1"):` **标定一段计算区域**，区域内的算子会被融合成一个超核。用户**不直接调用 `compile()`**——torchair 在编译期识别这些 scope 标记，收集区域内的算子并组织成 `op_list`，最终**触发**本讲的 `compile()`。
- **底层用法（`compile_sk.py`）**：基于 AscendC only，脱离框架。用户**直接** `super_kernel.compile(kernel_info, kernel_name)`，自己手动组织 `op_list`（手动给每个子算子的 `bin_path`/`json_path`）。

> ⚠️ 诚实说明：`superkernel_scope.py` 里**没有**直接出现 `compile()` 的调用——它调用的是 torchair 的 `tng.scope.super_kernel()`。从 scope 标记到 `compile()` 的桥接发生在 torchair / `asc_op_compile_base`（本仓库的外部依赖）内部，不在本仓库源码范围内，具体调用点**待确认**。要看 `compile()` 的**直接**调用，请看 `compile_sk.py`。

这两种用法的关系是：scope 示例展示「用户最常用的入口」，compile_sk 示例展示「compile() 真正被怎么调用」。两者最终都落到同一个 JIT `compile()`。

#### 4.3.2 核心流程

scope 示例的执行流程：

```
1. torch.npu.set_device(0)，准备若干 npu 张量（输入/权重/bias 等）
2. 定义 nn.Module 子类 Network：
       在 forward() 里用 with tng.scope.super_kernel("sk1"): 包住若干算子
       用 with tng.scope.super_kernel("sk2"): 包住另一组算子
       两个 scope 之间是普通（非融合）算子
3. model = torch.compile(model, fullgraph=True, backend=npu_backend, dynamic=False)
4. 跑一次 forward，打印 "execute sample success"
```

其中 `torch.compile` + `npu_backend` 是触发 torchair 编译的开关；torchair 看到 scope 标记后，会在编译期对 sk1/sk2 两个区域分别做 SuperKernel 编译。

#### 4.3.3 源码精读

先看 scope 示例里两个 scope 区域的声明：

[super_kernel/examples/super_kernel_base/superkernel_scope.py:74-80](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/examples/super_kernel_base/superkernel_scope.py#L74-L80) —— 第一个 scope `sk1`：用 `with tng.scope.super_kernel("sk1"):` 包住 `GroupedMatmul + GroupedMatmul + MoeGatingTopK` 三个算子，它们将被融合成一个超核。

[super_kernel/examples/super_kernel_base/superkernel_scope.py:86-92](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/examples/super_kernel_base/superkernel_scope.py#L86-L92) —— 第二个 scope `sk2`：包住 `DequantSwigluQuant + QuantBatchMatmulV3`；两个 scope 之间的 `reshape/square/concat` 是普通算子，不参与融合。

再看触发编译的 torch.compile：

[super_kernel/examples/super_kernel_base/superkernel_scope.py:99-100](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/examples/super_kernel_base/superkernel_scope.py#L99-L100) —— `torch.compile(model, fullgraph=True, backend=npu_backend, dynamic=False)`：这一步触发 torchair 编译，框架识别 sk1/sk2 标记并最终调用 JIT `compile()` 生成超核。

最后看底层示例里 `compile()` 的**直接**调用与 `kernel_info` 结构：

[super_kernel/examples/super_kernel_runtime_ascendc_only/compile_sk.py:334-352](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/examples/super_kernel_runtime_ascendc_only/compile_sk.py#L334-L352) —— 手动构造 `kernel_info`（含 `super_kernel_options` 与 `op_list`，`op_list` 每项给出 `stream_id`/`bin_path`/`json_path`），在 `OpContext('super_kernel')` 下调用 `super_kernel.compile(kernel_info, kernel_name)`。这是 `compile()` 最直接的调用样例。

> 对照记忆：scope 示例 = 「我用 `with` 标了一块区域，框架帮我编译」；compile_sk 示例 = 「我亲手把 op_list 喂给 `compile()`」。前者的 `op_list` 由框架收集，后者由人手写。

#### 4.3.4 代码实践（源码阅读型）

> 注意：本讲的实践任务原文是「标注 superkernel_scope.py 在何处调用 compile()」。如实说明：`superkernel_scope.py` **并不直接调用** `compile()`，它调用的是 torchair 的 `tng.scope.super_kernel()`。下面的实践据此调整为「标定 scope 位置 + 交叉对照真正的 compile() 调用」。

1. **实践目标**：分清 scope 标定与 `compile()` 直接调用，并能说出 op_infos 的至少两类元数据字段作用。
2. **操作步骤**：
   - 在 `superkernel_scope.py` 中标出两个 scope 的位置（第 74 行的 `sk1`、第 86 行的 `sk2`），说出它们各自包住了哪些算子。
   - 翻到 `compile_sk.py` 第 351 行，确认这里才是 `compile()` 的直接调用；对照它的 `kernel_info`（第 334–348 行）。
   - 回到 4.2 节，从 `SuperOperatorInfos` 中挑出两类字段，用一句话说明作用。
3. **需要观察的现象**：scope 区域内算子与区域外算子的区别；`compile_sk.py` 的 `op_list` 每项与 `SubOperatorInfos` 字段的对应关系。
4. **预期结果**：你能指出「scope 是用户入口、compile() 是 JIT 入口、二者通过 torchair 桥接」；并能说出例如 `op_list`（构成超核的子算子清单）与 `super_kernel_options`（编译优化选项，如 `early-start`/`split-mode`）这两类字段的作用。
5. **待本地验证**：若要在真实 NPU 上运行 scope 示例，需先安装 CANN 与 `.run` 增量包（见 u1-l4），运行命令为 `python3 superkernel_scope.py`，预期打印 `execute sample success`。本实践不强制上板。

#### 4.3.5 小练习与答案

**练习 1**：`superkernel_scope.py` 里的 `sk1` 和 `sk2` 之间有一段 `reshape/square/concat`（第 81–84 行），它们为什么没有被放进任何 scope？
**参考答案**：因为它们没有被 `with tng.scope.super_kernel(...)` 包住，所以是普通算子，不参与超核融合。这演示了 SuperKernel 的「区域可选」特性——用户可以精确控制哪些算子融合、哪些保持独立，避免把不适合融合的算子强行塞进超核反而损失性能。

**练习 2**：`compile_sk.py` 为什么需要 `with OpContext('super_kernel'):` 这一层上下文？
**参考答案**：底层编译基础设施（`asc_op_compile_base`）依赖 `OpContext` 区分当前在编译普通算子还是超核；`OpContext('super_kernel')` 会把后续 `compile()` 所需的上下文标记为「超核模式」，让 `global_var_storage`、编译配置等按超核路径处理。

---

## 5. 综合实践

把本讲的三条线索串起来，做一个「图纸翻译」练习（源码阅读型，无需硬件）。

**任务**：把 `superkernel_scope.py` 里 `sk1` 这个 scope 区域，翻译成 `compile_sk.py` 风格的手写 `kernel_info`。

1. **实践目标**：验证你是否同时理解了「scope 标定」与「compile() 的输入结构」。
2. **操作步骤**：
   - 列出 `sk1` 区域内的三个算子（`GroupedMatmul`、`GroupedMatmul`、`MoeGatingTopK`），假设它们各自已编译，产物为 `op1.json/op1.o`、`op2.json/op2.o`、`op3.json/op3.o`。
   - 仿照 `compile_sk.py` 第 334–348 行，写一个 `kernel_info` 字典：`op_list` 含三项，每项给出 `bin_path`/`json_path`/`stream_id`；`super_kernel_options` 任选一项（如 `early-start=0`）。
   - 写出对应的调用语句 `super_kernel.compile(kernel_info, "sk1")`。
   - 接着回答：这个 `kernel_info` 进入 `compile()` 后，会先被谁解析？（答：`SuperOperatorInfos`，它会把三项变成三个 `SubOperatorInfos` 放进 `info_base`。）
3. **需要观察的现象**：你写出的 `op_list` 项数是否等于 scope 内算子数；每项是否都包含了 sub_op_infos 必需的 `json_path`/`bin_path`。
4. **预期结果**：得到一个结构与 `compile_sk.py` 一致、但内容对应 `sk1` 三个算子的 `kernel_info`，并能说清它经 `SuperOperatorInfos` → `gen_super_kernel_file` → `compile_super_kernel` 的流转。
5. **待本地验证**：真实运行需要先手动编译出三个子算子的 `.o`/`.json`（参考 `compile_sk.py` 的 `compile_subkernel`），本练习只要求写出结构。

## 6. 本讲小结

- `compile()` 是 JIT 层总入口，遵循**解析元数据 → 生成 C++ 源文件 → 调用底层编译器**的三段式；入口处会重置全局存储、做 SoC 支持性检查，并靠 `.o` 是否存在做编译缓存。
- 元数据分两类：`SuperOperatorInfos`（op_infos）描述整个超核（`op_list`、整体 kernel_type、block_num、优化选项），`SubOperatorInfos`（sub_op_infos）描述单个子算子（`json_path`/`bin_path`、`stream_index`、事件依赖、生成的代码片段）；二者是聚合关系。
- 超核整体核类型由子算子的核类型与 max AIC/AIV 数量**自动汇总**决定（`get_summary_type_and_options` → `get_finale_type_and_block_num`），不能随意指定。
- 用户入口有两个层面：框架级的 `tng.scope.super_kernel()`（`superkernel_scope.py`，不直接调 `compile()`）与底层的 `super_kernel.compile()`（`compile_sk.py`，直接调用），二者最终都落到同一个 JIT `compile()`。
- `compile()` 不接受源码，只接受「已编译子算子清单」——子算子的算子逻辑已固化在各自 `.o` 里，`compile()` 只负责把它们按顺序、按核类型、按同步约束缝合。

## 7. 下一步学习建议

本讲讲清了 JIT 层「编译期」的入口与流程。SuperKernel 是双层结构，自然接下来应该转向 **AOT 层「运行期」**：

- 建议下一讲学习 **u10-l1（AOT 运行时架构）**：理解 `aclskOptimize`/`aclskScopeBegin`/`aclskScopeEnd` 这些公共 C 接口在运行时如何被调用、`SuperKernelGraph`/`Node` 的运行时表示，以及资源生命周期。
- 继续精读源码时，可顺着本讲的两条线索展开：① `super_kernel.py` 中 `gen_super_kernel_file()` 内部如何拼接同步/预取指令（涉及 Early-Start、PreLoad，对应 u2-l1 提到的四项深度优化）；② `super_kernel_op_infos.py` 的双流同步 pass（`insert_sync_by_stream_idx`/`optimize_sync_pass` 等），这是专家层融合决策的入口。
- 如果你想先看「用户怎么跑起来」，可回到 u1-l4 配好 CANN 与 `.run` 增量包后，运行 `python3 superkernel_scope.py` 验证 `execute sample success`。
