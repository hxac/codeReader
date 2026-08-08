# 参数体系全景：Megatron / SGLang / slime 三族

## 1. 本讲目标

slime 把 Megatron（训练）、SGLang（推理）、slime 自身（编排与 RL 算法）三套本不相关的系统缝合成一个命令行程序。这三家各自都带了一大批同名或冲突的命令行参数（例如 `--seed`、`--port`、`--tp-size` 同时出现在多处），如果直接把它们塞进同一个 `argparse` 解析器，必然大面积撞名报错。

本讲要回答的核心问题是：**slime 如何用一个 `parse_args()` 同时吃下三族参数、互不冲突，并在最后合并成一个干净的 `args` 命名空间？** 学完后你应当能够：

- 说清 `parse_args` 的「三阶段独立解析 → 合并 → 校验」流水线，以及为什么要分成三阶段。
- 解释 `--sglang-` 前缀透传机制：SGLang 的全部参数如何被自动加前缀、哪些参数被刻意「跳过」，以及前缀最终在何处被去掉。
- 理解 `--router-` 路由参数的处理方式与 `--sglang-` 的区别。
- 认识 `slime_validate_args` / `megatron_validate_args` / `sglang_validate_args` 三个校验函数的分工与先后顺序。
- 独立追踪一个具体参数（`--sglang-mem-fraction-static`）从命令行一路流转到 SGLang `ServerArgs` 的完整路径。

本讲承接 u1-l4（你已经会读 `scripts/run-qwen3-4B.sh` 的参数分组）和 u8-l1（你已经了解 `--sglang-config` 拓扑），把视线收回到参数是如何被「吃进来、整理好、分发出去」的工程机制上。

## 2. 前置知识

- **argparse 基础**：Python 标准库的命令行解析器。一个 `ArgumentParser` 通过 `add_argument` 注册选项；`parser.parse_args()` 解析 `sys.argv` 并返回一个 `Namespace` 对象，其属性名（dest）由选项名推导，例如 `--foo-bar` 默认对应属性 `foo_bar`。
- **`parse_known_args()`**：与 `parse_args()` 的区别在于，遇到**未注册**的选项时不报错，而是把它们连同对应值放进第二个返回值里返回。slime 用它来「只挑走自己关心的那部分参数，剩下的留给别人」。
- **`ignore_unknown_args=True`**：Megatron 解析器的一个开关，行为类似 `parse_known_args`，忽略自己不认识的选项。
- **命名空间合并**：`Namespace` 本质是一个装属性的对象，slime 用 `setattr(args, key, value)` 把多个命名空间「拍」到同一个 `args` 上。
- **monkey-patch（猴子补丁）**：在运行时临时替换某个对象的方法。slime 会临时替换 `parser.add_argument`，让 SGLang 注册参数时「被迫」加上前缀，注册完再恢复。
- **三族参数的来源**：Megatron 参数来自 `megatron.training.arguments`；SGLang 参数来自 `sglang.srt.server_args.ServerArgs` 与 `sglang_router.launch_router.RouterArgs`；slime 参数由 `get_slime_extra_args_provider` 生成。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [slime/utils/arguments.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py) | 参数中枢。定义 `parse_args` 三阶段流水线、全部 slime 专属参数、`slime_validate_args` 跨字段校验，以及 `reset_arg`/`_pre_parse_mode` 等辅助函数。 |
| [slime/backends/sglang_utils/arguments.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py) | SGLang 参数适配层。`add_sglang_arguments` 实现 `--sglang-` 前缀透传与跳过名单；`sglang_parse_args` 独立解析；`validate_args` 做 SGLang 侧校验。 |
| [slime/backends/megatron_utils/arguments.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/arguments.py) | Megatron 参数适配层。`megatron_parse_args` 调用 Megatron 原生解析器，注入 slime 的 `extra_args_provider`，并做 HF config 一致性校验与默认值设置。 |
| [slime/backends/sglang_utils/sglang_engine.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py) | SGLang 引擎封装。`_compute_server_args` 是**前缀被去掉**的最终位置，把带 `sglang_` 前缀的属性重新拼成无前缀的 `ServerArgs` 字段。 |
| [slime/ray/rollout.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py) | 路由器构造处。`--router-` 参数在这里经 `RouterArgs.from_cli_args` 还原。 |
| [train.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py) | 入口。仅一行 `args = parse_args()` 触发整个解析流水线。 |

---

## 4. 核心概念与源码讲解

### 4.1 parse_args 合并逻辑：三阶段独立解析再合并

#### 4.1.1 概念说明

「三族参数」的核心矛盾在于：Megatron 和 SGLang 是两个独立项目，它们的 CLI 参数**命名空间互相重叠**（例如都有 `--seed`、`--tp-size`、`--port`），却又**语义不同**（Megatron 的 `--tp-size` 是训练张量并行，SGLang 的是推理张量并行）。如果让它们共用一个 `argparse` 解析器，注册时就会因为同一个 dest 重复定义而直接崩溃。

slime 的解法是**「分而治之、各取所需、最后合并」**：

- 用**独立的解析器**分别解析 SGLang 和 Megatron+slime 两组，互不干扰；
- 用前缀（`sglang_`）在命名空间层面把 SGLang 的参数与 Megatron 的参数**隔离开**；
- 最后把所有命名空间 `setattr` 合并成**一个统一的 `args`**，并依次跑三道校验。

#### 4.1.2 核心流程

`parse_args` 的执行流程可以概括为「一预解析 + 两阶段解析 + 合并 + 三校验」：

```text
parse_args()
  │
  ├─ Phase 0  _pre_parse_mode()        # 极小预解析，抽出 4 个控制解析流程的开关
  │            └─ 决定 skip_sglang
  │
  ├─ Phase 1  sglang_parse_args()      # 独立解析器 + parse_known_args
  │            └─ 只吃 --sglang-* 前缀的参数 → sglang_ns
  │
  ├─ Phase 2  megatron_parse_args(     # Megatron 原生解析器
  │              extra_args_provider=    #  注入全部 slime 参数
  │                add_slime_arguments)  #  ignore_unknown_args=True 忽略 --sglang-*
  │            └─ 吃 Megatron + slime 参数 → args（主命名空间）
  │
  ├─ 合并：把 Phase 0 与 Phase 1 的属性 setattr 到 Phase 2 的 args 上
  │
  └─ 校验（按顺序）：
       slime_validate_args(args)         # slime 自己的跨字段校验
       megatron_validate_args(args)      # Megatron 原生校验
       sglang_validate_args(args)        # SGLang 侧校验
```

需要特别注意：Phase 1 和 Phase 2 的「分工」不是按**项目**切，而是按**前缀**切。Phase 1 用 `parse_known_args()` 把所有 `--sglang-*` 挑走，Phase 2 用 `ignore_unknown_args=True` 把剩下的 `--sglang-*`（以及预解析过的开关）当作「不认识」直接忽略。两边对同一批 `--sglang-*` 参数的处理是「一个吃、一个放」，从而避免重复定义。

#### 4.1.3 源码精读

入口 `parse_args` 定义在 [slime/utils/arguments.py:1547-1590](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1547-L1590)。下面逐段拆解。

**Phase 0：预解析。** 之所以要预解析，是因为有几个参数会**改变解析流程本身**（例如 `--debug-train-only` 时根本不需要 SGLang）。它们不能交给后续两个解析器重复注册：

```python
pre = _pre_parse_mode()
skip_sglang = pre.debug_train_only or pre.load_debug_rollout_data is not None
```

`_pre_parse_mode` 只用一个极简临时解析器抽 4 个开关，见 [slime/utils/arguments.py:1531-1544](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1531-L1544)：`--train-backend`、`--debug-rollout-only`、`--debug-train-only`、`--load-debug-rollout-data`。注意它们的注释明确说明「这些参数会在 Phase 2 之后合并进来」，因此 `add_slime_arguments` 里**故意不重复注册**它们。

**Phase 1：独立解析 SGLang 参数。** 见 [slime/utils/arguments.py:1558-1560](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1558-L1560)：

```python
sglang_ns = None
if not skip_sglang:
    sglang_ns = sglang_parse_args()
```

`sglang_parse_args()` 用一个独立的 `argparse.ArgumentParser(add_help=False)`，调用 `add_sglang_arguments` 把所有 SGLang 参数（带 `--sglang-` 前缀）注册进去，再用 `parse_known_args()` 只挑走带前缀的部分，见 [slime/backends/sglang_utils/arguments.py:188-211](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L188-L211)。返回的命名空间里所有属性都带 `sglang_` 前缀。

**Phase 2：解析 Megatron + slime 参数。** 见 [slime/utils/arguments.py:1565-1571](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1565-L1571)：

```python
args = megatron_parse_args(
    extra_args_provider=add_slime_arguments,
    skip_hf_validate=pre.debug_rollout_only,
)
```

`megatron_parse_args` 直接调用 Megatron 原生的 `_megatron_parse_args`，并把 slime 的全部参数通过 `extra_args_provider` 注入；关键是 `ignore_unknown_args=True`，让 Megatron 解析器**忽略**它不认识的 `--sglang-*` 与预解析开关，见 [slime/backends/megatron_utils/arguments.py:184-199](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/arguments.py#L184-L199)。

**合并。** 见 [slime/utils/arguments.py:1574-1580](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1574-L1580)：把 Phase 0 和 Phase 1 的属性逐个 `setattr` 到 Phase 2 的主 `args` 上。合并后 `args` 上同时存在 Megatron 参数（无前缀）、slime 参数（无前缀）和 SGLang 参数（带 `sglang_` 前缀），彼此靠前缀隔离、互不覆盖。

**校验。** 见 [slime/utils/arguments.py:1582-1588](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1582-L1588)，三道校验按固定顺序执行：先 `slime_validate_args`（依赖最全，且会派生 `use_critic` 等属性供后两道使用），再 `megatron_validate_args`，最后 `sglang_validate_args`。注意 `megatron_validate_args` 与 `sglang_validate_args` 都有 debug 模式下的跳过条件——例如 debug-train-only 时不校验 SGLang。

#### 4.1.4 代码实践

**实践目标**：不运行训练，仅通过阅读源码确认「Phase 1 吃、Phase 2 放」的对偶关系。

**操作步骤**：

1. 打开 [slime/backends/sglang_utils/arguments.py:210](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L210)，确认 Phase 1 用的是 `parse_known_args()`（返回 `(args, _)`，丢弃未知项）。
2. 打开 [slime/backends/megatron_utils/arguments.py:186](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/arguments.py#L186)，确认 Phase 2 传给 Megatron 的是 `ignore_unknown_args=True`。
3. 在 [slime/utils/arguments.py:1496-1498](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1496-L1498) 附近确认：`add_slime_arguments` 里**没有**注册 `--train-backend`、`--debug-train-only` 等开关，验证「预解析 + 合并」的设计。

**需要观察的现象**：Phase 1 的解析器不会因为遇到 `--tensor-model-parallel-size`（Megatron 参数）而报错，Phase 2 的解析器也不会因为遇到 `--sglang-mem-fraction-static` 而报错。

**预期结果**：两个解析器对「不属于自己」的参数都静默放过，靠前缀与 `ignore_unknown_args` 互不打架。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能把 SGLang 和 Megatron 的参数注册到同一个解析器？

**参考答案**：两者有大量同名参数（如 `--seed`、`--tp-size`、`--port`），但语义不同（训练并行 vs 推理并行）。同一个 `argparse` 解析器不允许重复注册同一 dest，会直接抛出冲突错误；即使强行复用也无法区分「这个 `tp_size` 到底喂给训练还是推理」。

**练习 2**：如果删除 Phase 0 的预解析，直接把 `--train-backend` 也交给 `add_slime_arguments` 注册，会发生什么？

**参考答案**：功能上仍可用，但失去了「先决定解析流程再解析」的分层。当前设计的价值在于：`skip_sglang` 等开关在 Phase 1 之前就确定，从而能在 debug-train-only 场景下完全跳过 SGLang 解析，避免在没有 SGLang 环境时也要 import SGLang。

---

### 4.2 add_sglang_arguments：--sglang- 前缀透传机制

#### 4.2.1 概念说明

SGLang 的 `ServerArgs` 是一个 dataclass，自带一个类方法 `ServerArgs.add_cli_args(parser)`，能把它**所有**字段一次性注册成 CLI 参数（如 `--mem-fraction-static`、`--chunked-prefill-size` 等）。slime 不可能手动逐个复制这些参数——SGLang 每次升级都可能新增字段。

slime 的优雅做法是：**临时把 `parser.add_argument` 替换成一个会自动加前缀的「代理」**，然后调用 `ServerArgs.add_cli_args(parser)`。SGLang 在不知情的情况下注册的每一个参数，都被代理偷偷改成了 `--sglang-xxx` 形式，从而与 Megatron 参数彻底隔离。这正是 u1-l1 提到的「SGLang-native」设计——上游升级近乎零成本，因为 slime 不维护 SGLang 参数的副本。

此外，有少量 SGLang 参数 slime **要自己接管**（如 `model_path`、`tp_size`、`port`），不能让它们以 `--sglang-` 形式暴露。这些被列入一份「跳过名单」，代理遇到它们直接 `return`，不注册。

#### 4.2.2 核心流程

`add_sglang_arguments` 的工作流：

```text
add_sglang_arguments(parser)
  │
  ├─ 1. 先注册路由参数 add_sglang_router_arguments(parser)
  │      └─ RouterArgs.add_cli_args(parser, use_router_prefix=True)  # 用 --router- 前缀
  │
  ├─ 2. 保存原始方法：old_add_argument = parser.add_argument
  │
  ├─ 3. 定义代理 new_add_argument_wrapper(*flags, **kwargs)：
  │      ├─ 计算规范名 canonical_name（从 --foo-bar 推 foo_bar）
  │      ├─ 若 canonical_name 在 skipped_args 名单 → return（不注册）
  │      ├─ 给每个 flag 加前缀：--foo-bar → --sglang-foo-bar
  │      └─ 若显式给了 dest，也给 dest 加 sglang_ 前缀
  │
  ├─ 4. 替换：parser.add_argument = new_add_argument_wrapper
  │
  ├─ 5. ServerArgs.add_cli_args(parser)   # SGLang 在「被骗」状态下注册全部参数
  │
  └─ 6. 恢复：parser.add_argument = old_add_argument
```

两个关键概念：

- **skipped_args（跳过名单）**：这些 SGLang 字段由 slime 在别处显式管理，不暴露 `--sglang-` 形式，包括 `model_path`（用 `--hf-checkpoint`）、`tp_size`（由 `--rollout-num-gpus-per-engine` 推导）、`port`/`nnodes`/`node_rank`（由 Ray 拓扑决定）、`random_seed`（用 Megatron 的 `--seed`）等。
- **dest 自动推导**：若某个参数没有显式 `dest`，argparse 会从加前缀后的 flag `--sglang-mem-fraction-static` 自动推导出属性名 `sglang_mem_fraction_static`。这正是前缀能一路带到命名空间的关键。

#### 4.2.3 源码精读

`add_sglang_arguments` 定义在 [slime/backends/sglang_utils/arguments.py:38-141](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L38-L141)。

**路由参数单独走 `--router-` 前缀。** 见 [slime/backends/sglang_utils/arguments.py:42](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L42) 调用 `add_sglang_router_arguments`，后者用 SGLang 原生的 `RouterArgs.add_cli_args(parser, use_router_prefix=True)`，由 SGLang 自己负责加 `--router-` 前缀（见 [slime/backends/sglang_utils/arguments.py:31](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L31)）。这是 `--router-` 与 `--sglang-` 处理方式的根本区别：`--router-` 前缀由 SGLang 的 RouterArgs 内置支持，不需要 monkey-patch。

**跳过名单。** 见 [slime/backends/sglang_utils/arguments.py:48-66](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L48-L66)，注释把它们分成 `# memory`、`# distributed` 等几组，说明每类被跳过的原因。

**代理的核心三步。** 见 [slime/backends/sglang_utils/arguments.py:68-114](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L68-L114)：

```python
# (1) 推导规范名用于查名单
canonical_name_for_skip_check = stem.replace("-", "_")   # --foo-bar -> foo_bar
if canonical_name_for_skip_check in skipped_args:
    return                                                # 跳过，不注册

# (2) 给 flag 加前缀
prefixed_item = f"--sglang-{original_flag_stem}"          # --foo-bar -> --sglang-foo-bar

# (3) 给 dest 加前缀（若有显式 dest）
if not original_dest.startswith("sglang_"):
    final_kwargs["dest"] = f"sglang_{original_dest}"
```

**触发注册并恢复。** 见 [slime/backends/sglang_utils/arguments.py:116-118](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L116-L118)：先替换 `parser.add_argument` 为代理，调用 `ServerArgs.add_cli_args(parser)` 让 SGLang 注册全部字段，**注册完立即恢复**原方法。这个「替换—注册—恢复」的三明治结构是 monkey-patch 的标准安全写法，保证副作用只局限在 SGLang 注册期间。

#### 4.2.4 代码实践

**实践目标**：亲手验证「前缀是在注册时被加上去的」，并理解名单的作用。

**操作步骤**：

1. 在能 import sglang 的环境里，运行下面这段最小复现脚本（**示例代码**，非项目原有代码）：

   ```python
   # 示例代码：复刻 add_sglang_arguments 的前缀逻辑
   import argparse

   parser = argparse.ArgumentParser()
   old = parser.add_argument
   skipped = {"tp_size", "port"}

   def wrapped(*flags, **kwargs):
       stem = flags[0][2:].replace("-", "_")        # --mem-fraction-static -> mem_fraction_static
       if stem in skipped:
           return                                    # 跳过
       new_flags = [f"--sglang-{f[2:]}" for f in flags if f.startswith("-")]
       old(*new_flags, **kwargs)

   parser.add_argument = wrapped
   # 模拟 SGLang 注册两个参数
   parser.add_argument("--mem-fraction-static", type=float, default=0.9)
   parser.add_argument("--tp-size", type=int, default=1)        # 命中名单，被跳过
   parser.add_argument = old

   ns = parser.parse_args(["--sglang-mem-fraction-static", "0.4"])
   print(vars(ns))   # 预期：{'sglang_mem_fraction_static': 0.4}，没有 tp_size
   ```

2. 对照 [slime/backends/sglang_utils/arguments.py:85-86](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L85-L86) 的 `return`，确认名单命中后该参数彻底不注册。

**需要观察的现象**：解析结果里 `mem_fraction_static` 被存为 `sglang_mem_fraction_static`（带前缀），而 `tp_size` 完全不存在。

**预期结果**：`{'sglang_mem_fraction_static': 0.4}`。

**待本地验证**：若当前 sglang 版本的 `ServerArgs` 字段名或默认值有变化，实际注册的参数集合可能略有不同；本脚本只演示前缀机制本身。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `model_path` 必须在跳过名单里，而不能以 `--sglang-model-path` 暴露？

**参考答案**：因为 slime 用 `--hf-checkpoint` 统一指定模型路径（它同时给 Megatron 转 HF 权重、给 tokenizer、给 SGLang 初始化用）。若再暴露 `--sglang-model-path`，用户可能填两个不一致的路径。slime 在 `_compute_server_args` 里把 `args.hf_checkpoint` 直接赋给 `ServerArgs.model_path`（见 4.3.3），所以必须从 CLI 层面屏蔽掉它。

**练习 2**：`--router-` 前缀为什么不需要像 `--sglang-` 那样用 monkey-patch？

**参考答案**：因为 SGLang 的 `RouterArgs.add_cli_args` 原生支持 `use_router_prefix=True` 参数，会自己加 `--router-` 前缀；而 `ServerArgs.add_cli_args` 没有这样的前缀选项，slime 只能用代理临时改写。

---

### 4.3 前缀在哪里被去掉：namespace → ServerArgs

#### 4.3.1 概念说明

前缀解决了「注册与解析阶段的隔离」，但 SGLang 的 `ServerArgs` 构造函数只认**无前缀**的字段名（如 `mem_fraction_static`，而不是 `sglang_mem_fraction_static`）。因此在推理引擎启动前，必须有一个「去前缀」的环节：遍历 `ServerArgs` 的全部字段，对每个字段名 `xxx`，去 `args` 上找 `args.sglang_xxx`，找到就填进无前缀的 `kwargs["xxx"]`。

这个环节就是 `_compute_server_args`，它是整条参数链路的**最后一公里**。理解它，才能完整回答本讲开篇的追踪问题。

#### 4.3.2 核心流程

`_compute_server_args` 把一个 slime 的 `args` 翻译成 SGLang 能懂的字段字典，优先级从低到高是：

```text
kwargs 构造（三来源叠加）
  │
  ├─ 来源 A：slime 显式管理的字段（无前缀，直接写死）
  │     model_path = args.hf_checkpoint
  │     tp_size / dp_size / pp_size / ep_size   # 由拓扑推导
  │     host / port / nnodes / node_rank ...     # 由 Ray 分配
  │     enable_memory_saver = args.offload_rollout
  │
  ├─ 来源 B：前缀透传字段（去前缀搬运）
  │     for attr in dataclasses.fields(ServerArgs):
  │         if hasattr(args, "sglang_" + attr.name) and attr.name not in kwargs:
  │             kwargs[attr.name] = getattr(args, "sglang_" + attr.name)
  │
  └─ 来源 C：--sglang-config 的 per-group overrides（最高优先级）

→ ServerArgs(**kwargs)   # 用无前缀的 kwargs 真正构造 ServerArgs
```

注意来源 B 的两个守卫：`attr.name not in kwargs` 保证来源 A（slime 显式管理的字段，如 `tp_size`）不会被前缀字段覆盖；这正是 `tp_size` 当初要进跳过名单的原因——它在来源 A 里由 `--rollout-num-gpus-per-engine` 推导，不能被一个 `--sglang-tp-size` 抢走。

#### 4.3.3 源码精读

`_compute_server_args` 定义在 [slime/backends/sglang_utils/sglang_engine.py:523-535](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L523-L535)。

**来源 A：slime 显式管理的字段。** 见 [slime/backends/sglang_utils/sglang_engine.py:544-571](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L544-L571)，这里直接写死了 `model_path`、`tp_size`、`dp_size`、`pp_size`、`ep_size`、`enable_memory_saver` 等字段的取值来源，例如 `tp_size` 由 `_gpus_per_engine // pp_size` 推导、`enable_memory_saver` 取自 `args.offload_rollout`。

**来源 B：去前缀搬运（本讲最关键的两行）。** 见 [slime/backends/sglang_utils/sglang_engine.py:595-600](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L595-L600)：

```python
for attr in server_arg_fields:
    if worker_type == "decode" and attr.name == "enable_hierarchical_cache":
        continue
    if hasattr(args, f"sglang_{attr.name}") and attr.name not in kwargs:
        kwargs[attr.name] = getattr(args, f"sglang_{attr.name}")
    unused_keys.discard(attr.name)
```

**这就是前缀被去掉的精确位置**：对每个 `ServerArgs` 字段 `attr.name`（如 `mem_fraction_static`），用 `f"sglang_{attr.name}"` 拼出带前缀的属性名去 `args` 取值，再以**无前缀**的 `attr.name` 作为 key 存入 `kwargs`。

**真正构造 ServerArgs。** 见 [slime/backends/sglang_utils/sglang_engine.py:191](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L191)：`self.process = launch_server_process(ServerArgs(**server_args_dict))`。此时 `server_args_dict` 里所有 key 都已是无前缀的标准 `ServerArgs` 字段名。

**路由参数的对称还原。** `--router-` 参数在 [slime/ray/rollout.py:1044](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1044) 经 `RouterArgs.from_cli_args(args, use_router_prefix=True)` 还原成 `RouterArgs` 对象——同样是一个「去前缀」步骤，只是交给 SGLang 原生方法完成。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：完整追踪 `--sglang-mem-fraction-static 0.4` 从命令行到 `ServerArgs` 对象的全程，指出前缀在哪一步被去掉。这是本讲规格指定的核心任务。

**操作步骤**：按下表逐站核对源码，填出每一站的「所在文件:行」「属性名」「值」。

| 站点 | 阶段 | 文件:行（需你核对） | 属性名 | 值 |
| --- | --- | --- | --- | --- |
| 0 | 命令行 | `scripts/run-qwen3-4B.sh`（SGLANG_ARGS） | `--sglang-mem-fraction-static` | `0.4` |
| 1 | 注册（加前缀） | `sglang_utils/arguments.py:117`（经代理 `:89-94`） | flag → `--sglang-mem-fraction-static`，dest → `sglang_mem_fraction_static` | — |
| 2 | Phase 1 解析 | `sglang_utils/arguments.py:210`（`parse_known_args`） | `sglang_mem_fraction_static` | `0.4` |
| 3 | 合并进主命名空间 | `utils/arguments.py:1578-1580`（`setattr`） | `args.sglang_mem_fraction_static` | `0.4` |
| 4 | **去前缀** | `sglang_engine.py:598-599` | key 从 `sglang_mem_fraction_static` → `mem_fraction_static` | `0.4` |
| 5 | 构造 ServerArgs | `sglang_engine.py:191`（`ServerArgs(**server_args_dict)`） | `server_args.mem_fraction_static` | `0.4` |

**需要观察的现象**：在第 4 站，`getattr(args, "sglang_mem_fraction_static")` 取到的 `0.4` 被赋给 `kwargs["mem_fraction_static"]`（key 无前缀）。

**预期结果**：前缀在 **第 4 站（`sglang_engine.py:598-599`）** 被去掉；最终 `ServerArgs` 对象上 `mem_fraction_static == 0.4`。

**可选动手验证**（需 sglang，**示例代码**）：

```python
import argparse, dataclasses
from sglang.srt.server_args import ServerArgs

# 模拟合并后的 slime args 命名空间
args = argparse.Namespace()
args.sglang_mem_fraction_static = 0.4

# 复刻 sglang_engine.py:595-600 的去前缀循环
kwargs = {}
for attr in dataclasses.fields(ServerArgs):
    if attr.name == "mem_fraction_static" and hasattr(args, f"sglang_{attr.name}"):
        kwargs[attr.name] = getattr(args, f"sglang_{attr.name}")
print(kwargs)   # 预期 {'mem_fraction_static': 0.4} —— 前缀已去掉
```

**待本地验证**：本脚本依赖 sglang 可被 import；不同 sglang 版本下 `ServerArgs` 是否含 `mem_fraction_static` 字段需以本机为准。

#### 4.3.5 小练习与答案

**练习 1**：如果用户既传了 `--sglang-mem-fraction-static 0.4`，又在 `--sglang-config` 的某 server group 的 `overrides` 里写了 `mem_fraction_static: 0.6`，最终生效的是哪个？

**参考答案**：生效的是 `0.6`。因为 overrides（来源 C）在 [sglang_engine.py:604-620](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L604-L620) **最后**应用，且直接 `kwargs[normalized_key] = value` 覆盖，优先级最高。这与 u8-l1 讲的「per-group overrides 优先级最高」一致。

**练习 2**：为什么 `tp_size` 不能也走「来源 B 前缀搬运」，而必须在来源 A 里写死？

**参考答案**：因为训练侧的张量并行与推理侧的张量并行必须协调，slime 选择由 `--rollout-num-gpus-per-engine`（结合 `pp_size`）**统一推导**推理 `tp_size`，而不是让用户分别指定。若允许 `--sglang-tp-size` 覆盖，会破坏这种协调；所以 `tp_size` 既在跳过名单里（不暴露 CLI），又在来源 A 里 `not in kwargs` 守卫中被保护。

---

### 4.4 slime_validate_args：跨字段校验与派生默认值

#### 4.4.1 概念说明

三族参数合并到同一个 `args` 后，需要一个地方处理「**跨字段的约束**」——这些约束没有任何一族参数能单独表达。例如：

- `--kl-coef`（KL 系数）非零时，必须有 `--ref-load`（参考模型检查点）。
- `--use-dynamic-batch-size` 时，必须设 `--max-tokens-per-gpu`。
- `--colocate`（共卡）时，要自动推导 `offload_train`/`offload_rollout` 和 `rollout_num_gpus`。
- `--advantage-estimator ppo` 时，要派生 `use_critic=True`。

这些就是 `slime_validate_args` 的职责。它和另外两个校验函数的分工是：`slime_validate_args` 管「slime 语义层面的跨字段约束与派生」；`megatron_validate_args` 管「Megatron 原生校验 + HF config 一致性」；`sglang_validate_args` 管「SGLang 拓扑推导（如 tp_size）与互斥检查」。

#### 4.4.2 核心流程

`slime_validate_args` 是一个长长的「if 校验 + setattr 派生」序列，可按职责归类：

```text
slime_validate_args(args)
  │
  ├─ 解析 eval 数据集：args.eval_datasets = _resolve_eval_datasets(args)
  │
  ├─ KL / OPD 约束：kl_coef/use_kl_loss → 必须有 ref_load；use_opd → opd_type 校验
  │
  ├─ 检查点加载判定：load 是 megatron 还是 hf，据此设 no_load_optim/finetune/start_rollout_id
  │
  ├─ 一大批跨字段 assert：
  │     kl_coef 与 kl_loss_coef 互斥、reinforce 系需 normalize_advantages、
  │     use_dynamic_batch_size 需 max_tokens_per_gpu、release_train 的多项前提 …
  │
  ├─ 派生属性：
  │     rollout_external、use_critic、critic_*_num_gpus、offload_train/rollout、
  │     colocate 下的 rollout_num_gpus …
  │
  └─ 供需守恒校验（仅当显式设了 --num-steps-per-rollout）：
        global_batch_size = rollout_batch_size * n_samples_per_prompt // num_steps_per_rollout
```

其中供需公式是 u1-l4 讲过的核心约束：

\[
\text{rollout\_batch\_size} \times \text{n\_samples\_per\_prompt}
= \text{global\_batch\_size} \times \text{num\_steps\_per\_rollout}
\]

#### 4.4.3 源码精读

`slime_validate_args` 定义在 [slime/utils/arguments.py:1714](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1714) 起。

**派生 `use_critic` 与 critic 卡数。** 见 [slime/utils/arguments.py:1847-1850](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1847-L1850)：只有 `advantage_estimator == "ppo"` 才需要 critic，且 critic 强制与 actor 用相同卡数——这是 u4-l1「critic 复用 actor 同组 GPU」的实现依据。

**colocate 的默认值兜底。** 见 [slime/utils/arguments.py:1875-1891](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1875-L1891)，colocate 下若 `offload_train`/`offload_rollout` 为 `None` 则默认开 `True`，若 `rollout_num_gpus` 未设则等于 actor 总卡数。这与 u1-l4「colocate 自动设 rollout_num_gpus 并开启 offload」的结论对应。

**供需守恒校验。** 见 [slime/utils/arguments.py:1907-1915](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1907-L1915)：只有显式设了 `--num-steps-per-rollout` 时才推导并 assert `global_batch_size`；否则信任用户手填的 `--global-batch-size`、不做校验——这解释了 u1-l4 提到的「只有显式设 num-steps-per-rollout 才自动推导或 assert」。

**delta 权重同步的多项前提。** 见 [slime/utils/arguments.py:1995-2011](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1995-L2011)：`--update-weight-mode=delta` 必须搭配 `--update-weight-transport=disk`、禁止 `--colocate`、且必须提供 `--update-weight-local-checkpoint-dir`。这些 assert 与 u5-l1/u5-l2 的「四象限选型」完全对应——校验函数是架构约束的**守门人**。

**SGLang 侧校验。** `sglang_validate_args`（即 [sglang_utils/arguments.py:144-185](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L144-L185) 的 `validate_args`）负责推导 `sglang_tp_size`（由 `rollout_num_gpus_per_engine // sglang_pp_size`）并检查 PD 分离 / sglang-config / 外部引擎三者的互斥关系。

#### 4.4.4 代码实践

**实践目标**：通过阅读校验代码，理解 slime 如何在参数层把「架构约束」固化下来。

**操作步骤**：

1. 在 [slime/utils/arguments.py:1787](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1787) 找到 `kl_coef` 与 `kl_loss_coef` 的互斥 assert，阅读其错误信息。
2. 在 [slime/utils/arguments.py:1808-1811](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1808-L1811) 找到 `use_dynamic_batch_size` 需 `max_tokens_per_gpu` 的 assert，并注意紧接着的 `log_probs_max_tokens_per_gpu` 默认值兜底。
3. 思考：如果不做这些 assert，错误会延迟到训练的哪一步才暴露？

**需要观察的现象**：几乎每条 assert 都附带清晰的错误提示，告诉用户该补哪个参数。

**预期结果**：这些 assert 把「配置错误」的发现时机从「训练中途崩溃」提前到「启动即失败」，大幅缩短排错回路。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `slime_validate_args` 必须在 `megatron_validate_args` 和 `sglang_validate_args` **之前**执行？

**参考答案**：因为 `slime_validate_args` 会派生 `use_critic`、`rollout_external`、`offload_train` 等属性，后两个校验函数（以及后续的编排代码）依赖这些派生属性。若顺序反了，后两者可能读到未派生的默认值而误判。

**练习 2**：`--update-weight-mode=delta --colocate` 为何被禁止？

**参考答案**：colocate 模式下训练与推理共卡，权重同步走 CUDA IPC（只传显存句柄，不拷数据），此时维护 delta 所需的「快照 + diff + 编码」纯属开销且无意义；错误信息也明确说明了这一点（见 [utils/arguments.py:2001-2006](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L2001-L2006)）。这是 u5-l1 四象限里「delta 限 disk 且禁 colocate」的代码出处。

---

## 5. 综合实践

**任务**：给一个新同事写一份「slime 参数流转一页纸」，把本讲的三条主线串起来。

要求完成以下三件事：

1. **画一张端到端时序图**：从 `train.py:98` 的 `parse_args()` 开始，依次画出 Phase 0 → Phase 1 → Phase 2 → 合并 → 三校验，再到引擎启动时 `_compute_server_args` 去前缀、`ServerArgs(**kwargs)` 构造。在每个箭头上标注「数据载体」（预解析 namespace / `sglang_ns` / 主 `args` / `server_args_dict`）。

2. **选一个 SGLang 参数做完整追踪**（除 `mem_fraction_static` 外任选一个，如 `--sglang-chunked-prefill-size` 或 `--sglang-disable-cuda-graph`）：填出 4.3.4 那张表的五个站点，确认它不在跳过名单（[sglang_utils/arguments.py:48-66](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L48-L66)），并指出它的前缀同样在 [sglang_engine.py:598-599](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L598-L599) 被去掉。

3. **找一条校验规则并解释其架构含义**：在 `slime_validate_args` 里任选一条 assert（例如 `release_train` 要求 `update_weight_mode=full` + `transport=disk`），说明它对应到前面哪一讲（U5 权重同步）的哪个设计决策。

**验收标准**：时序图能独立解释「为什么三族参数不会撞名」；追踪表能准确指出前缀剥离发生在 `sglang_engine.py:598-599`；所选校验规则能与上游架构讲义对上号。

---

## 6. 本讲小结

- slime 用**三阶段独立解析再合并**解决三族参数撞名：Phase 0 预解析控制开关、Phase 1 用 `parse_known_args` 吃掉 `--sglang-*`、Phase 2 用 `ignore_unknown_args=True` 放过 `--sglang-*`，最后 `setattr` 合并成统一 `args`。
- `--sglang-` 前缀通过**临时替换 `parser.add_argument`** 实现：SGLang 的 `ServerArgs.add_cli_args` 在「被骗」状态下注册的每个参数都被自动加前缀，使上游升级零成本。
- 一份**跳过名单**让 slime 自己接管 `model_path`/`tp_size`/`port` 等关键字段，避免与 slime 顶层参数冲突。
- 前缀在推理引擎启动前的 `_compute_server_args`（[sglang_engine.py:598-599](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L598-L599)）被去掉：遍历 `ServerArgs` 字段，把 `args.sglang_xxx` 搬成无前缀的 `kwargs["xxx"]`，再 `ServerArgs(**kwargs)` 构造。
- `--router-` 路由参数走不同路径：由 SGLang 原生 `RouterArgs.add_cli_args(use_router_prefix=True)` 加前缀，由 `RouterArgs.from_cli_args(use_router_prefix=True)` 去前缀。
- `slime_validate_args` 是跨字段约束与派生默认值的「守门人」，必须先于另两个校验执行，把配置错误提前到启动即失败。

## 7. 下一步学习建议

- 想看参数如何驱动**拓扑**：进入 u8-l1（SGLang 拓扑与 sglang-config），看 `--sglang-config` 的 overrides 如何在 `_compute_server_args` 的来源 C 处覆盖前缀字段。
- 想看参数如何驱动**权重同步选型**：复习 u5-l1/u5-l2，对照本讲 `slime_validate_args` 里 delta/full × nccl/disk 的 assert，理解「四象限选型」的代码出处。
- 想看参数如何被**编排层消费**：阅读 `slime/ray/placement_group.py`（u2-l2），看 `actor-num-gpus-per-node`、`rollout-num-gpus` 等参数如何决定 placement group 布局。
- 想动手扩展参数体系：若要新增一个 slime 专属参数，只需在 `get_slime_extra_args_provider` 的对应 `add_xxx_arguments` 里 `parser.add_argument`，并在 `slime_validate_args` 里补上必要的跨字段校验——无需改动 SGLang 或 Megatron 的解析逻辑。
