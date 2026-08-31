# u8-l4 算子工程工具链：review-op、optimize-op、refactor-op

## 1. 本讲目标

学完本讲，你应该能够：

1. 运行 `tools/review_op.py`、`tools/optimize_op.py`、`tools/refactor_op.py` 三个确定性检查器，并读懂它们输出的 `PASS / GAP / N-A / MANUAL / RECOMMENDATION` 五种状态与退出码语义。
2. 说出三个工具各自的检查域与阶段划分：review-op 的 support/test/bench/docs 四域，optimize-op 的 preflight/evidence/summary 三阶段，refactor-op 的 assess/verify 两模式。
3. 解释 preflight/evidence 阶段门禁如何与「黄金法则」（先基准、先金标、先画像，再改代码）配合，防止性能优化引入正确性回归与隐性内存增长。
4. 理解 refactor-op 用 shingle 指纹 + Jaccard 相似度检测近似重复内核的算法原理，以及 verify 阶段「奇偶校验门」（frozen test/bench surface、API/ABI、特征矩阵不变）的实现方式。
5. 按 `OPTIMIZATION_GUIDELINES.md` 的纪律（先基准后改码）规划一次合规的算子优化。

## 2. 前置知识

本讲是第八单元「二次开发」的收尾，站在 u8-l1（mkop 脚手架与 make_op.py 门禁）、u7-l3（nvbench 基准与基线）的肩膀上。开始前请确认理解这些概念：

- **确定性检查器（deterministic checker）**：不联网、不读时钟、不用随机数，同一棵源码树永远产出逐字节相同的报告。这三个工具都是「检查器只报告、不修文件」的只读设计，修复动作由外层 agent/skill 执行。
- **Finding（发现项）**：每条检查结果的最小单元，含 `id`（如 SUP-10）、`status`（五种状态之一）、`summary`、`evidence`（`文件:行号` 级证据）、`guideline`（引用的指南）、`fix`（命名修复动作）。
- **五种状态**：
  - `PASS`：检查通过；
  - `GAP`：硬失败，可动作的缺陷，**使工具退出码非零**；
  - `N-A`：对本算子不适用（如 tensor-only 算子的 VarShape 检查）；
  - `MANUAL`：机器判不了，需要人读代码/跑命令，检查器只指出位置；
  - `RECOMMENDATION`（缩写 REC）：建议性改进，人可否决。
- **算子的「四层文件」与外围**（u1-l4、u5-l1 已建立）：`Op<Op>.h`（C API）、`Op<Op>.hpp`（C++ 类）、`src/cvcuda/priv/Op<Op>.cpp|cu`（实现与内核）、`python/mod_cvcuda/operators/Op<Op>.cpp`（绑定），加上 `tests/`、`bench/`、`bench/config/operators/<op>.json`。三个工具的全部检查都落在这张文件地图上。
- **基线（baseline）与 SKU**（u7-l3 已建立）：`bench/config/operators/<op>.json` 内嵌按 case-key × SKU（如 A100、H100）组织的耗时基线；基线只能由 CI 播种、绝不许手编。
- **Limitations 契约表**（u3-l1 已建立）：算子公开 C 头中 Doxygen 注释里的 Data Layout / Channels / Data Type 表，是支持矩阵的唯一权威。本讲的工具会**机器解析这张表**并与其他表面对账。

一句话定位：**mkop 负责让新算子「长出来」，本讲的三个工具负责让存量算子「活得健康」**——覆盖是否齐全（review）、优化是否合规（optimize）、代码是否冗余（refactor）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tools/review_op.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py) | 单算子覆盖评审检查器：support/test/bench/docs 四域，任何 GAP 退出非零 |
| [.agents/guidance/REVIEW_OP_GUIDELINES.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/REVIEW_OP_GUIDELINES.md) | review-op 的清单「实体」：每个检查项的定义、探针、PASS 条件与修复 |
| [tools/optimize_op.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py) | 优化战役「完成定义」（DoD）检查器：preflight / evidence / summary 三阶段 |
| [.agents/guidance/OPTIMIZATION_GUIDELINES.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/OPTIMIZATION_GUIDELINES.md) | 优化纪律：四条黄金法则、工作流、验收/三振/停止标准、结果摘要格式 |
| [tools/refactor_op.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py) | 单算子冗余检查器：assess（建议）与 verify（奇偶校验硬门） |
| [.agents/guidance/REFACTOR_OP_GUIDELINES.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/REFACTOR_OP_GUIDELINES.md) | 重构规则目录：RED-* 检查项、VER-* 奇偶门、 curated 阈值与允许清单 |
| [.agents/tools/operator_source_map.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/tools/operator_source_map.py) | 共享模块：算子名 → 内核源文件归属（legacy 文件命名、共享内核清单） |
| [.agents/tools/binding_api.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/tools/binding_api.py) | 共享模块：Python 绑定的保守 API 快照（注册/签名/类型别名） |
| [.agents/tools/review_op_data.json](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/tools/review_op_data.json) | review-op 的 curated 数据：tensor-only/varshape-only 名单、bench 豁免、basic 期望集 |

阅读路线建议：先读两个 GUIDELINES 的「How to use / 状态表」（各约 60 行），再对照 tools/ 下同名 Python 的 `main()` 与各 `check_*` 函数，最后用 Flip 实测。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：4.1 三个工具的共同设计契约；4.2 review-op 四域覆盖评审；4.3 optimize-op 阶段门禁与优化纪律；4.4 refactor-op 冗余检测与奇偶校验门。

### 4.1 模块一：三个检查器的共同设计契约

#### 4.1.1 概念说明

三个工具解决的是同一类问题的三个侧面：**LLM 时代的 agent 会「说」它做完了，仓库需要机器可复核的「完成」定义**。CV-CUDA 的做法是把每个领域的完成标准写成一份 Markdown 指南（人读），再配一个确定性 Python 检查器（机器跑），指南与检查器一一对应：

| 指南（人读） | 检查器（机器跑） | 完成的定义 |
|---|---|---|
| REVIEW_OP_GUIDELINES.md | tools/review_op.py | 重跑检查器：零 GAP、零未解决 MANUAL |
| OPTIMIZATION_GUIDELINES.md | tools/optimize_op.py | `--phase evidence` 重跑：零 GAP、零 MANUAL |
| REFACTOR_OP_GUIDELINES.md | tools/refactor_op.py | `--phase verify`：零 GAP 且 VER-6/7 人工证明转绿 |

三条共同契约：

1. **指南是实体，检查器是实现，skill 是薄包装**。清单内容只在指南里写一次，`.agents/skills/` 下的技能文件只负责调度，避免多处漂移。
2. **只读 + 幂等 + 确定性**。检查器永不写源码文件（`--fix`/`--apply` 都只是提示外层 agent 去修），不联网、不读时钟、不掷骰子。确定性不是风格偏好，而是「重跑即证明」这条完成定义的前提——如果两次运行报告不同，重跑就不能当作证据。
3. **证据必须落到 `文件:行号`**。每条 Finding 的 `evidence` 字段携带字面证据（解析出的声明、grep 命中的行），而不是「看起来没问题」。

#### 4.1.2 核心流程

三个工具共享同一个骨架流程：

```text
main(argv)
  ├─ argparse 解析 <Operator> 与 --domain/--phase/--format/--out
  ├─ resolve_op()            # 算子名 → 全部相关文件路径（见下）
  ├─ load_curated()          # 加载 curated 数据（JSON 或从指南 Markdown 解析）
  ├─ check_*() 逐项检查       # 产出 Finding 列表（status/evidence/fix）
  ├─ render_md()/render_json()  # 渲染报告（md 给人，json 给机器/上层工具）
  └─ return 退出码             # 是否非零由「哪些状态算阻塞」决定（各工具不同！）
```

算子名解析是第一步，也是三个工具共用的套路：`<Operator>` 传 PascalCase（如 `Flip`），工具推导出三个名字——`op`（小写，bench/config 与 Python 测试文件名用的 stem）、`Op`（C++ 类与测试套件 stem）、`pyname`（Python 函数名，可能与 op 不同，如 `HqResize` → `hq_resize`）。

#### 4.1.3 源码精读

先看 review_op.py 如何把一个名字展开成整张文件地图。[tools/review_op.py:209-269](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py#L209-L269) 的 `resolve_op`：在 `src/cvcuda/include/cvcuda/` 下大小写不敏感地匹配 `Op*.h` 得到 `Op`，再按命名约定拼出 `.hpp`、绑定、C++ 测试、Python 测试、双语言基准与 bench 配置的路径；priv 内核则先找 `Op<Op>.cu/.cpp`，再到 legacy 目录用 `legacy_belongs()` 认领归属文件，最后补上 `SHARED_KERNEL_SOURCES` 显式清单（如 `gaussian` 认领 `filter.cu`）。

`legacy_belongs` 的「最长名优先」规则在 [operator_source_map.py:34-42](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/tools/operator_source_map.py#L34-L42)：文件名去下划线小写后按前缀匹配，但若存在更长的算子名也匹配（`gaussian` vs `gaussian_noise.cu`），则判给最长者——避免 `gaussian` 错认 `gaussian_noise.cu`（u1-l4 提过的高斯模糊与高斯噪声是两个算子）。共享内核清单在 [operator_source_map.py:45-64](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/tools/operator_source_map.py#L45-L64)。

Finding 数据结构在 [tools/review_op.py:72-80](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py#L72-L80)：`id/domain/status/summary/evidence/guideline/fix` 七个字段，三个工具各有一份几乎相同的定义（optimize_op.py 的在 [tools/optimize_op.py:98-106](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L98-L106)，refactor_op.py 的在 [tools/refactor_op.py:98-106](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L98-L106)）——这是刻意的独立副本而非共享库，让三个工具可以各自演进。

退出码语义**三个工具不同**，这是最容易被忽视的差异：

- review_op.py：[tools/review_op.py:2251](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py#L2251) `return 1 if any(f.status == GAP ...)`——只有 GAP 非零，MANUAL 只展示；
- optimize_op.py：[tools/optimize_op.py:1827-1828](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L1827-L1828) `blocking = {GAP, MANUAL} if phase == "evidence" else {GAP}`——evidence 阶段 MANUAL 也阻塞；
- refactor_op.py：[tools/refactor_op.py:1254](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L1254) 只有 GAP 非零，而 GAP 只会在 verify 阶段出现（assess 只发 REC/MANUAL，永远退出 0——重构是改进不是门禁）。

报告渲染的判定逻辑见 [tools/review_op.py:2157-2176](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py#L2157-L2176)：每个域汇总出 `PASS / GAPS / NEEDS-REVIEW` 三种 verdict（有 GAP 是 GAPS，只有 MANUAL 是 NEEDS-REVIEW），并明确写出「Completion = 重跑零 GAP 零未解决 MANUAL」。

#### 4.1.4 代码实践

**实践目标**：不改任何代码，验证「确定性」承诺与退出码语义。

**操作步骤**：

1. 在仓库根目录连续两次运行同一命令并把输出重定向：
   ```bash
   python3 tools/review_op.py Flip --domain support > /tmp/r1.md
   python3 tools/review_op.py Flip --domain support > /tmp/r2.md
   diff /tmp/r1.md /tmp/r2.md && echo IDENTICAL
   ```
2. 观察退出码：`python3 tools/review_op.py Flip --domain support; echo "exit=$?"`。
3. 换成 JSON 格式便于机器处理：`python3 tools/review_op.py Flip --domain test --format json | python3 -m json.tool | head -40`。

**需要观察的现象**：两次报告逐字节一致（diff 无输出）；退出码与报告中 GAP 计数联动。

**预期结果**：`IDENTICAL` 被打印；退出码为 0 或 1，取决于 Flip 的 support 域当前是否有 GAP。**具体判定结果待本地验证**（本讲义写作环境未获准执行该命令）。

#### 4.1.5 小练习与答案

**练习 1**：为什么三个工具坚持「检查器只读、修复由外层 agent 执行」而不是让 `--fix` 直接改文件？

**参考答案**：确定性重跑是完成定义的证据来源。如果检查器自己改文件，「检查 → 修复 → 复查」就混在一个有副作用的黑箱里，无法保证幂等（第二次运行面对的是被改过的树）。拆开后检查器是纯函数：同一棵树 → 同一份报告，agent 修完再跑一次即可机械地证明收敛。REVIEW_OP_GUIDELINES.md 第 43-50 行还列出三条不可逾越的修复规则（不许伪造基线、不许悄悄放宽容差、缺失的布局能力是作者的工作），这些判断需要人/agent 介入，不适合塞进检查器。

**练习 2**：`MANUAL` 状态在 review_op.py 和 optimize_op.py 的 `--phase evidence` 中对退出码的影响有何不同？为什么要这样设计？

**参考答案**：review_op.py 中 MANUAL 不影响退出码（只有 GAP 非零）；optimize_op.py 的 evidence 阶段把 MANUAL 也算阻塞（`blocking = {GAP, MANUAL}`）。原因：review 是覆盖巡检，MANUAL 项（如「确认金标独立性」）可以带病存在、留待人工；而 evidence 是优化 MR 合入前的最终门禁，「完成」的定义就是零 GAP 零未解决 MANUAL，任何悬而未决的人工项都可能掩盖性能回归，所以宁可挡住。

**练习 3**：`resolve_op("Flip")` 会解析出哪些文件路径？先凭 u1-l4 的命名规律手写，再对照源码核对。

**参考答案**：header=`src/cvcuda/include/cvcuda/OpFlip.h`、hpp=`.../OpFlip.hpp`、pybind=`python/mod_cvcuda/operators/OpFlip.cpp`、test_cpp=`tests/cvcuda/system/TestOpFlip.cpp`、test_py=`tests/cvcuda/python/test_opflip.py`、bench_cpp=`bench/cpp/ops/BenchFlip.cpp`、bench_py=`bench/python/ops/bench_flip.py`、bench_cfg=`bench/config/operators/flip.json`、priv 包含 `src/cvcuda/priv/OpFlip.cpp` 与 legacy 归属的 `src/cvcuda/priv/legacy/flip.cu`（本讲义已核实前八个文件全部存在）。

### 4.2 模块二：review_op.py——单算子的四域覆盖评审

#### 4.2.1 概念说明

`review_op.py` 回答一个问题：**这个算子在 support（支持面）、test（测试）、bench（基准）、docs（文档）四个域的覆盖是否齐全且互相一致**。它不验证数值正确性本身（那是 u7-l1 的黄金参考测试的事），它验证的是「该有的东西有没有、几张表面上说的是不是同一件事」。

核心检查思路是**跨表面对账**：同一个支持矩阵（container × layout × dtype × channels）被写在四处——C 头的 Limitations 表、`.hpp` 的重载、Python 绑定的 `m.def`、测试与基准的实际覆盖。工具把每一处都解析成集合，然后互相对：

- **SUP 域**：C API 是否声明了 Tensor/VarShape 容器（SUP-1/2）、`.hpp` 重载是否与 C API 一致（SUP-3）、Python 是否绑定 allocating + `_into` 双变体（SUP-4）、Limitations 表是否存在且可解析（SUP-5/6/7/8）、priv 层是否有执行期拒绝非法组合的守卫（SUP-9）、planar 布局默认契约（SUP-10）、跨表面一致性（SUP-11）。
- **TST 域**：独立 CPU 金标（TST-1）、Tensor/VarShape 正确性正例（TST-2/3）、参数化套件（TST-4）、矩阵镜像覆盖（TST-5）、负例套件（TST-6）、等价布局位精确奇偶测试（TST-7）、容差纪律（TST-8）、金标独立性与边界充分性（TST-9）、确定性输入（TST-10）、Python 四项（TST-11..14）。
- **BEN 域**：双语言基准文件与注册（BEN-1/2）、manifest（BEN-3）、配置与 tier（BEN-4）、每个配置有真实的 layout 轴（BEN-5）、planar 对比覆盖（BEN-6）、基线完整性（BEN-7）、行数一致性（BEN-8）、内部校验器探针（BEN-9）、双语言配置对齐（BEN-10）、运行期质量（BEN-11）、basic 档最低门槛（BEN-14）、以及建议性的覆盖统计与同尺寸对比（BEN-13/15/16/SIZE）。
- **DOC 域**：operator_list 行（DOC-1）、autofunction 指令（DOC-2）、C++ API 引用（DOC-3）、Limitations 与代码/测试一致（DOC-4）、docstring（DOC-5）、Doxygen 解释「为什么」（DOC-6）、SPDX 头（DOC-7）。

#### 4.2.2 核心流程

以 support 域为例的数据流：

```text
读 Op<Op>.h 原文
  ├─ 正则解析全部 Submit 签名 → 按「主数据输入句柄」分类 Tensor / VarShape（SUP-1/2）
  ├─ 正则解析 Doxygen Limitations 表 → {layouts, channels, dtypes} 集合（SUP-5..8）
  ├─ 解析 'Planar image layouts: Not applicable + Reason' 局部声明 → planar 政策（SUP-10）
  ├─ 读 .hpp / 绑定，与 C API 容器集对账（SUP-3/4/11）
  └─ grep priv 文件找 ERROR_INVALID_ARGUMENT 等守卫痕迹（SUP-9，永远 MANUAL）
```

「主数据输入」的判定规则值得记：**取算子句柄与 stream 参数之后出现的第一个 Tensor/TensorBatch/ImageBatch 句柄**。这排除了张量输出与辅助张量的干扰——像 `PadAndStack` 这类混合容器老 API 的 Submit 是「图像批输入 + 张量输出」，不能因为返回张量就误判为支持 Tensor 输入。

#### 4.2.3 源码精读

**Submit 签名解析**。[tools/review_op.py:148-176](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py#L148-L176) 用一个跨行正则抓取 `CVCUDA_PUBLIC NVCVStatus cvcuda<Op>[VarShape|ImageBatch|TensorBatch]?Submit(...)` 声明；[tools/review_op.py:179-201](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py#L179-L201) 的 `detect_c_api_containers` 再按上述「第一个句柄」规则分类。注意它特意用主句柄而非函数名后缀分类，并且大小写不敏感——因为存在 `HQResize → cvcudaHqResizeSubmit` 这种 C 符号大小写与文件名不一致的历史。

**Limitations 表解析**。[tools/review_op.py:342-379](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py#L342-L379) 的 `parse_limitations`：先切出 `Limitations:` 到 `Output:`（或注释结束）之间的 Input 区段，再用三个正则分别抽 `Data Layout: [kNHWC, ...]`、`Channels: [1, 3, 4]`、`32bit Float | Yes` 形式的 dtype 行；另有兜底处理散文式 planar 声明（如 HQResize 的 `NVCV_TENSOR_[N]CHW (planar, 2D only)`）。u2/u3 里你人工读的契约表，在这里变成了机器可对账的集合。

**SUP-10 planar 默认契约**。[tools/review_op.py:382-430](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py#L382-L430) 的 `parse_planar_policy` 解析算子头文件中唯一允许的局部豁免声明 `Planar image layouts: Not applicable` + 紧随其后的 `Reason:`；[tools/review_op.py:433-486](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py#L433-L486) 的 `planar_policy_verdict` 给出四种裁决：声明合法（PASS）、声明格式错误（GAP）、既声明 planar 又声明不适用（自相矛盾，GAP）、什么都没声明（默认要求 NCHW/CHW，GAP）。这正对应 AGENTS.md 的仓库不变量「图像算子默认双布局，不适用须声明原因」。

**永远 MANUAL 的项**。看 [tools/review_op.py:712-745](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py#L712-L745) 会发现 SUP-9 的两个分支**都返回 MANUAL**——找到守卫痕迹时是「有守卫，但声明与执行是否一致需人读」，没找到时是「没找到明显守卫，请人工确认」。同类还有 TST-9、BEN-9、BEN-11、DOC-3/4/6。设计意图：机器负责定位，人负责语义判断。

**BEN-14 basic 档最低门槛**。[tools/review_op.py:1741-1781](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/review_op.py#L1741-L1781) 的 `basic_floor` 检查 basic 档必须覆盖：RGB（3 通道 dtype）× Tensor（若支持）× VarShape（若支持）× 交错 NHWC × 平面 NCHW（若 planar）。注意它 是**轴覆盖下限，不要求笛卡尔积全开**——指南明确说不支持的组合必须缺席，而不是加一行跳过的误导行。

**Flip 的真实数据**（本讲义已核实，供实践对照）：

- [src/cvcuda/include/cvcuda/OpFlip.h:61](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L61) 声明 `Data Layout: [kNHWC, kHWC, kNCHW, kCHW]` → SUP-10 应为 PASS，且 planar=True 会连带要求 TST-7 与 BEN-6；
- [tests/cvcuda/system/TestOpFlip.cpp:311](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L311) 有 `TEST_P(OpFlipPlanar, tensor_matches_interleaved)`，[:324](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L324) 有 `OpFlip_Negative` 套件 → TST-7/TST-6 的探针应命中；
- [bench/config/operators/flip.json](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/config/operators/flip.json) 有 20 个 tier 条目、29 处 NCHW（含 8 处 NCHW_FAKE）、完整 A100/H100 基线（如 [:29-49](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/config/operators/flip.json#L29-L49) 的第一个 case-key），basic 档含 uchar3/float3 的 NHWC Tensor+VarShape。

#### 4.2.4 代码实践

**实践目标**：对 Flip 运行全部四域评审，收集所有 MANUAL/GAP 输出并逐条注明现状。

**操作步骤**：

1. 逐域运行（比一次 `--domain all` 更容易对照）：
   ```bash
   python3 tools/review_op.py Flip --domain support
   python3 tools/review_op.py Flip --domain test
   python3 tools/review_op.py Flip --domain bench
   python3 tools/review_op.py Flip --domain docs
   ```
2. 建一张三列表格：`id | status | 现状说明`。对每个 MANUAL/GAP，打开其 evidence 指向的文件行，写一句「现状」——例如 `SUP-9 MANUAL：priv/legacy/flip.cu 的 dtype 分派表就是守卫，声明与执行一致` 或 `BEN-9 MANUAL：尚未运行 validate_baselines.py`。
3. 重点核对：工具报告的 `lay=[...] dt=[...] ch=[...]` 是否与你在 OpFlip.h 里肉眼读到的 Limitations 表一致。

**需要观察的现象**：每条 Finding 下方的 `evidence:`（文件:行号）与 `fix:`（GAP 项）字段；每个域末尾的 `→ domain verdict: PASS/GAPS/NEEDS-REVIEW (n GAP, m MANUAL)`。

**预期结果**（基于源码逻辑与上述 Flip 工件的分析，**具体判定待本地验证**）：

- 必然出现的 MANUAL：SUP-9、TST-5（matrix_mirror 两分支都是 MANUAL）、TST-9、BEN-9、BEN-11、DOC-3、DOC-4、DOC-6——这八个是结构性 MANUAL；
- SUP-1/2/4/5、TST-2/3/4/6/7、BEN-1/2/3、DOC-1/2/5/7 预期 PASS（对应文件均已存在）；
- BEN-6/BEN-14 取决于 flip.json 中 NCHW 与 FakePlanar 配对的精确匹配及 basic 档是否含 NCHW 行，需以实际输出为准；
- 若出现 GAP，按其 `fix:` 字段逐条记录整改方案（但不要在评审实践里真去改 bench 基线——基线只能 CI 播种）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 SUP-1/SUP-2 用「主数据输入句柄」而不是「函数名里有没有 VarShape」来分类容器支持？

**参考答案**：存在两类历史特例：一是 C 符号大小写与文件名不一致（HQResize → `cvcudaHqResizeSubmit`），靠名字匹配会漏；二是混合容器老 API（如 CropFlipNormalizeReformat、PadAndStack）的通用 `Submit` 以 ImageBatch 为输入、Tensor 为输出，函数名没有 VarShape 字样但语义上就是 VarShape 入口。按「算子与 stream 参数后的第一个容器句柄」分类对两类都稳健。

**练习 2**：一个新算子头文件写了 `Data Layout: [kNHWC, kHWC]` 且没有任何 planar 声明，review_op.py 会给出什么裁决？两种整改路径分别是什么？

**参考答案**：SUP-10 判 GAP「Planar image layouts are required by default」。路径一：实现 NCHW/CHW 支持，把布局加进 Limitations 表（同时 TST-7 会要求 `Op<X>Planar.*_matches_interleaved` 奇偶测试、BEN-6 要求原生 NCHW 基准配置）；路径二：若该算子的张量不表示图像（如纯坐标列表），在头文件 Limitations 旁写恰好一次 `Planar image layouts: Not applicable` 并附非空 `Reason:`。两条路都走（既声明 NCHW 又声明不适用）会被判自相矛盾的 GAP。

**练习 3**：BEN-7「基线完整性」检查什么？为什么缺口不能手编补上？

**参考答案**：它把配置里每个条目展开成全部期望 case-key（`expected_case_keys_for_entry`），与 `sku_map.json` 中每个 SKU 做笛卡尔积，逐一核对内嵌 baselines 中存在对应指标；任何缺失即 GAP 且 fix 写明「requires CI」。不许手编是因为基线是性能回归门（ODO-7）的比对基准——手编一个数等于自己给自己发及格证，所以只能走 CI 基线工作流（`bench/_internal/update_baseline.py` 导入真实运行产物）。

### 4.3 模块三：optimize_op.py——优化战役的门禁与「先基准后改码」纪律

#### 4.3.1 概念说明

`optimize_op.py` 是**单算子优化战役的「完成定义」（Definition of Done）检查器**。它不替你选优化、不替你写内核——那是 agent + GPU 的优化循环的事；它把守战役的两端：

- **`--phase preflight`（起飞前）**：在动第一行内核代码之前，确认正确性覆盖、基准覆盖、基线、画像工具就绪。防止「改完才发现没有回归测试兜底」。
- **`--phase evidence`（完成时）**：逐项核对优化 MR 的硬证据——回归测试与逐像素相等证据、基线已更新、线索穷尽（三振出局）、API/ABI 未动、提交卫生（`perf:` 前缀）、基线无回归、review/refactor 门已过、内存增长在限、算子范围未漂移、FakePlanar 配对仍精确、绑定层收益有统计显著性。
- **`--phase summary`（摘要）**：从基准工件生成或刷新 MR 描述中有界的 `cvcuda-optimize-summary:v1` 摘要块。

背后的纪律是 `OPTIMIZATION_GUIDELINES.md` 的**四条黄金法则**：①正确性强制（整数输出位精确 `EXPECT_EQ`，浮点用声明容差；覆盖缺失就先补覆盖或别碰该内核）；②基准证据强制（每个声称的收益都要有名目硬件的 before/after，回归面上任何基准不得劣化超出噪声带）；③CI 证据强制（本地数字只指导迭代，评审要 CI 在参考硬件上的运行）；④限制内存增长（默认不变，10 MB 内且无新运行期 CUDA 分配/释放路径自动放行，超出需人工评审）。

「先基准后改码」的具体体现：preflight 的 PRE-1/PRE-2 **直接复用 review_op.py 的 test/bench 域**，PRE-3 确认基线已捕获——三者不绿就不该开始优化；指南还要求基准覆盖与优化改动**分开提交**（同一提交会抹掉优化前基线）。

#### 4.3.2 核心流程

优化战役的完整生命周期（指南 Workflow 五阶段 × 工具门禁）：

```text
1 调研定界   全基准面先行 → 按实测余量排序；不改 NCHW_FAKE 参考行
2 建立覆盖   缺覆盖先补基准(单独 MR) → 逐内核金标 → 捕获基线
             ┌─────────────── optimize_op.py --phase preflight ───────────────┐
             │ PRE-1 复用 review_op test 域(TST-1/2)   正确性回归覆盖        │
             │ PRE-2 复用 review_op bench 域(BEN-4..7) 基准覆盖+基线          │
             │ PRE-3 bench config 内含 baselines       基线已捕获            │
             │ PRE-4 PATH 上有 ncu/nsys                画像工具可用          │
             │ PRE-5 API/ABI 保持不变(在 evidence 复核) 提醒项                │
             └───────────────────────────────────────────────────────────────┘
3 画像后动手  ncu/nsys 命名瓶颈(访存/计算/发射/启动/同步五类) → 提出可证伪假设
4 优化循环    画像→选线索→假设→单点改动→评估→refactor 门→锁定(perf: 提交)或三振
             └─ 每次锁定后重新画像；连续三次新线索失败 = 当前面穷尽
5 收尾 MR     基线更新 + v1 摘要 + evidence 门禁全绿
             ┌─────────────── optimize_op.py --phase evidence ───────────────┐
             │ ODO-0..12: 回归+逐像素证据/基线更新/摘要格式/线索穷尽/          │
             │ API-ABI/提交卫生/基线回归门/review-refactor 门/内存门/          │
             │ 范围门/FakePlanar 门/绑定收益显著性门                           │
             └───────────────────────────────────────────────────────────────┘
```

三振出局（new-lead strike budget）机制：每次锁定胜果后清零失败计数；连续三个新线索被击落（结果在噪声带内/指标不动/测试失败/回归面劣化/内存超标未获批）即宣告当前算子-配置面穷尽。`at ridge`（约 90%+ BWUtil/SOL，接近硬件极限）与「测量背书的 triage」是两种合法的「不优化」结论——凭感觉的「ROI 低」不合法。

#### 4.3.3 源码精读

**PRE-1/PRE-2 复用 review_op**。[tools/optimize_op.py:211-225](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L211-L225) 的 `review_op_findings` 以子进程跑 `review_op.py <Op> --domain <d> --format json` 并解析 findings；[tools/optimize_op.py:297-344](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L297-L344) 把指定 id（TST-1/2 与 BEN-4..7）中的 GAP 升格为 PRE-1/PRE-2 的 GAP，fix 写明「先在**独立的非性能提交**里补齐覆盖再优化」。这是工具间组合的范例：optimize 不重复实现覆盖检查，而是消费 review 的结论。

**PRE-3 基线存在性**。[tools/optimize_op.py:346-367](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L346-L367)：读 `bench/config/operators/<op>.json`，正则确认 `"baselines"` 块非空。**PRE-4 画像工具**。[tools/optimize_op.py:369-389](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L369-L389) 用 `shutil.which` 探测 `ncu`/`nsys`，缺失时 MANUAL（允许在 MR 里记录不可用而非硬拦）。**PRE-5** 恒为 MANUAL 的提醒：API/ABI 约束在 evidence 阶段由 ODO-5 机器复核。

**ODO-1 逐像素硬证据**。[tools/optimize_op.py:432-485](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L432-L485)：同时要求 review_op 的 TST-1/2 无 GAP **且** MR 摘要里勾选了「Pixelwise equality to reference」且勾选行包含真实的断言代码（`_has_pixelwise_assertion` 识别 `EXPECT_EQ/EXPECT_NEAR`、`np.testing.assert_array_equal` 等，见 [tools/optimize_op.py:267-289](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L267-L289)）、提及独立参考（reference/oracle/gold/host/cpu）、并有通过结果——一个光秃秃的 `[x]` 勾选不算证据。

**ODO-5 API/ABI 门**。[tools/optimize_op.py:1289-1376](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L1289-L1376)：对公开头与绑定做 `git diff`，任何命中 `CVCUDA_PUBLIC|operator()|Submit(` 的增删行立即 GAP「那是特性工作，不是优化」；绑定文件若变更，则用 `binding_api_snapshot` 对比基线与候选的 m.def 注册、可调用签名、类型别名三元组，完全一致才放行（优化允许改绑定**实现**，不许改绑定**面**）。

**ODO-6 提交卫生**。[tools/optimize_op.py:1458-1545](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L1458-L1545)：遍历 merge-base 以来的提交，凡触碰该算子实现的提交，主题行必须 `perf:` 前缀；唯一的例外是「`test(<op>):`（仅改 tests/ 且含算子专属回归）紧跟着 `fix(<op>):`」的成对修复——战役中顺手修出的内核正确性 bug 必须保持 bugfix 工作流的形状。

**ODO-9 内存门**。[tools/optimize_op.py:1048-1159](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L1048-L1159)：解析 MR 描述中恰好一个可见的 `## Memory footprint` 段（[L1009-1045](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L1009-L1045) 的解析器会剥掉 HTML 注释与代码围栏里的假段），要求 `Peak attributable increase: <N> B`、`New runtime CUDA allocations/frees: no|yes`、非占位符 `Evidence:` 三行各恰好一次；超过 [L95](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L95) 的 `MEMORY_AUTO_ACCEPT_BYTES = 10_000_000`（10 MB）或声明了新分配路径 → MANUAL（需人工评审，且评审未决期间不得提交）。

**ODO-7 基线回归门**。[tools/optimize_op.py:1379-1455](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L1379-L1455)：实际子进程运行 `bench/_internal/validate_baselines.py --reject-regressions-from <base>`，对全部已提交基线集拒绝同 key 变慢——把 u7-l3 的 compare_to_baseline 思路固化成 MR 门禁。

**ODO-10 范围门与「失败即拒绝」**。[tools/optimize_op.py:1216-1286](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L1216-L1286)：把改动集归属到算子（bench 配置路径 / 绑定路径 / priv 最长前缀，见 [L1189-1213](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L1189-L1213)），要求一个 MR 一个算子；次级算子豁免需内部策略文件 `ci/optimization_secondary_scope_policy.py` 放行，而该文件**在 OSS 镜像中故意缺席**，导入失败时的兜底函数一律返回 False（[L76-87](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L76-L87)）——缺失的策略必须拒绝所有多算子例外，这是「fail closed」设计。

**ODO-12 绑定层收益显著性**。[tools/optimize_op.py:640-785](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L640-L785)：仅改绑定的战役，C++ 耗时可能纹丝不动而 Python 开销真实下降，所以要求每个优化 case 在每个参考 SKU 上的开销削减量超过合并标准误：

\[ SE = \sqrt{\frac{\sigma^2_{\text{before}}}{n_{\text{before}}} + \frac{\sigma^2_{\text{after}}}{n_{\text{after}}}} \]

其中 σ 取 `gpu_gap_stddev_us`（同一工件内 Python 减 C++ 配对差的无偏标准差），见 [L740-748](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/optimize_op.py#L740-L748)。削减量 ≤ SE 就不是被证明的优化——即使摘要中位数是正的。

#### 4.3.4 代码实践

**实践目标**：对 Flip 运行 preflight，逐条总结它检查了哪些前置条件。

**操作步骤**：

1. 运行：`python3 tools/optimize_op.py Flip --phase preflight`（在 main 分支干净树上，预期「无优化被检测」是常态——preflight 本就不依赖改动集）。
2. 对照输出把五个 PRE 项抄进表格：`PRE-id | 检查什么 | 数据来源 | 结果`。
3. 追踪复用链：手动运行 `python3 tools/review_op.py Flip --domain test --format json`，找出其中 id 为 TST-1、TST-2 的两条，与 preflight 输出里 PRE-1 的 evidence 对照，确认它是同一条结论的转述。
4. 再运行 `python3 tools/optimize_op.py Flip --phase evidence --base main`，观察 ODO-0（无实现改动 → N-A）与其余 ODO 项在「尚未优化」的树上的表现；注意 evidence 阶段 MANUAL 也计入阻塞、退出码为 1 是**预期行为**而非故障。

**需要观察的现象**：PRE-1/2 的 evidence 字段是否引用 review_op 的 finding 摘要；PRE-3 是否指向 `bench/config/operators/flip.json`；PRE-4 在无 GPU 工具的环境下是否降级为 MANUAL 而非 GAP；退出码。

**预期结果**（**待本地验证**）：PRE-3 应为 PASS（flip.json 含非空 baselines 块，本讲义已核实 [:28-49](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/config/operators/flip.json#L28-L49)）；PRE-1 依赖 TST-1/2（u7-l1 已确认 TestOpFlip.cpp 有 FlipCPU 金标与 tensor_correct_output 类用例，预期 PASS）；PRE-2 依赖 BEN-4..7，其中 BEN-6 的 FakePlanar 精确配对以实际输出为准；PRE-4 取决于本机是否装有 ncu/nsys；PRE-5 恒 MANUAL。preflight 只有 GAP 影响退出码，MANUAL 不影响。

#### 4.3.5 小练习与答案

**练习 1**：为什么指南要求「补基准覆盖」与「优化改动」必须分开成两个提交/MR？

**参考答案**：基线是 before/after 比较的「before」。若同一提交既加基准行又改内核，优化前的基线从未被单独记录，收益就无法归因——你优化后的数字既是新基准又是优化结果，自证循环。分开提交让 CI 能在「仅基准」的 SHA 上播种优化前基线，优化 MR 再与之对比（这也是 ODO-7 按 `--base` 拒绝同 key 变慢的前提）。

**练习 2**：一次尝试让目标 kernel 快了 8%，但同算子的 VarShape 姊妹配置慢了 15%（超出噪声带）。按 Accept/Drop 标准该如何处置？

**参考答案**：Strike（击落/回退）。成功标准是联立的：目标指标向假设方向移动、收益清噪声带、**回归面（含同算子其他配置与共享内核的其他算子）无超噪声劣化**、内存合规、refactor 门干净——任何一条不满足即回退，并在工作日志记一行：试了什么、哪个指标没动/哪个配置回归、回归多少。指南还提醒：一次成功的胜果会重置三振计数，但被击落的线索不能靠无限换姿势复活，同一想法的机械修正（如修启动配置 bug）可再测一次，不得变成开放式搜索。

**练习 3**：`--phase summary` 生成的摘要块为什么把 `optimized_cases` 藏在 HTML 注释里，而把结论性文字留在可见 Markdown 中？

**参考答案**：职责分离（见指南 Metadata Contract，[OPTIMIZATION_GUIDELINES.md:330-383](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/OPTIMIZATION_GUIDELINES.md#L330-L383)）：隐藏 JSON 只放机器可核实的字段（算子、状态、基线/候选 SHA、精确展开的优化 case key），供 evidence 门做确定性校验（key 不得重复/缺失/未知、candidate 必须是当前 HEAD、baseline 必须是其祖先）；人的评估、证据和学习要点写在可见部分，是给人读的权威报告。把统计数字塞进隐藏对象会诱导手编，而生成器会重算并拒绝不一致。

### 4.4 模块四：refactor_op.py——shingle 指纹冗余检测与 verify 奇偶门

#### 4.4.1 概念说明

`refactor_op.py` 找的是 **SonarQube 复制粘贴检测（CPD）表达不了的、算子作用域内的语义冗余**：Tensor 与 VarShape 两个近乎复制的内核该合成一个模板、手写的布局校验该换成共享访问器、本地助手函数与 `cuda_tools` 里的正牌工具重名、只剩定义没有调用者的死代码。它与 Sonar 互补：Sonar 管全库的重复「量与位置」，refactor-op 管算子内「该合一而没合一」的结构性问题；per-op 基准与姊妹测试脚手架天生近似，被显式排除在外（它们是冻结面）。

两种模式语义完全不同：

- **assess（默认）**：只读建议报告，只发 RECOMMENDATION/MANUAL/PASS/N-A，**永远退出 0**——重构是改进不是门禁，值不值得做由人判断。
- **verify**：已应用重构的**奇偶校验硬门**。重构的定义就是「不改变任何可观察行为」，所以五条确定性腿（冻结测试面、冻结基准面、API/ABI、声明特征矩阵、测试覆盖矩阵 base vs 工作区必须完全一致）任何一条 GAP 都意味着你做的不是重构而是特性/度量漂移；位精确这一腿交给冻结测试去跑（MANUAL 证明）。

#### 4.4.2 核心流程

近似重复检测的算法链（assess 的 impl 域）：

```text
对每个 priv 源文件
  ├─ _strip_code        剥注释与字符串内容（保留换行与花括号 → 行号稳定）
  ├─ extract_blocks     括号平衡地提取函数/内核体（跳过 namespace/class/控制流花括号），
  │                     仅保留归一化后 ≥ 6 行的块
  ├─ normalize_line     每行去首尾空白、连续空白折叠为一个空格
  ├─ shingle_hashes     滑动窗口取连续 k=5 行拼串 → blake2b 64 位哈希（每块一组指纹）
  └─ near_duplicate_pairs  任意两块计算 shingle 集合的 Jaccard 相似度
                           相似度 ≥ 0.80 → 近似重复 → RED-1 建议
```

Jaccard 相似度的定义：两个集合 A、B 的

\[ J(A, B) = \frac{|A \cap B|}{|A \cup B|} \]

取值 0 到 1；两块代码的 5-gram 指纹集合重合度达到 80% 即判「近似重复」。k-gram（shingle）技术把「逐行相等」放松为「局部窗口相等」，对变量改名、少量增删行都稳健。

verify 的五条确定性腿全部来自工件（`git diff <base>` 与解析出的矩阵），不依赖运行：

| 腿 | 检查 | 通过条件 |
|---|---|---|
| VER-1 | 冻结测试面 | 算子的 C++/Python 测试文件零 diff |
| VER-2 | 冻结基准面 | bench 源码/配置/基线零 diff |
| VER-3 | API/ABI | 公开头无签名 diff；绑定三元组快照（m.def 注册、可调用签名、类型别名）相同 |
| VER-4 | 特征矩阵 | Limitations 表解析出的 input/output layouts/channels/dtypes 集合相同 |
| VER-5 | 覆盖矩阵 | 测试宏多重集 + 参数化值行计数相同 |
| VER-6 | 位精确 | MANUAL：本地构建后跑 `cvcuda_test_system --gtest_filter='Op<Op>*'` 全绿 |
| VER-7 | 冗余已消除 | MANUAL：重跑 assess 确认对应 RED-* 消失 |

#### 4.4.3 源码精读

**确定性哈希**。[tools/refactor_op.py:194-199](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L194-L199) 的 `_stable_hash` 用 `hashlib.blake2b` 而非 Python 内建 `hash()`——内建 hash 对 str 有进程级随机盐（PYTHONHASHSEED），同一文件两次运行指纹不同，会毁掉「逐字节相同报告」的承诺。注释里明确写了这是确定性保证的落点。

**注释/字符串剥离**。[tools/refactor_op.py:203-239](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L203-L239) 的 `_strip_code` 是一个手写单遍状态机：剥 `//` 与 `/* */` 注释、把字符串/字符字面量的**内容**清空但保留引号与换行——这样行号不漂移、花括号结构不被字符串里的假括号干扰。

**块提取与控制流豁免**。[tools/refactor_op.py:285-330](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L285-L330) 的 `extract_blocks` 用「每层开括号记名字」的栈在任意嵌套深度（命名空间里的函数、pybind 的 lambda）提取函数体；[tools/refactor_op.py:333-350](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L333-L350) 的 `_block_open` 只在括号前签名以 `)` 收尾且名字不是控制关键字时给块命名——[L266-283](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L266-L283) 的 `_CTRL_KW` 集合特意包含 `constexpr`，因为 `if constexpr (...)` 会让 `constexpr` 成为括号前最后一个 token，不豁免的话嵌套控制流会与外层函数配成假重复。

**三档检查域**。impl 域（[tools/refactor_op.py:509-626](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L509-L626)）：RED-1 近似重复内核（REC，fix 指路 `DoBrightnessContrast<isPlanar>` 式模板统一）、RED-4 手写 `TENSOR_NCHW || TENSOR_NHWC` 校验链（MANUAL，应改用 TensorDataAccess）、RED-5 无访问器包裹的手工 stride 算术（MANUAL）。api 域（[L630-670](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L630-L670)）：RED-6 对绑定文件里的函数/lambda 体跑同一套指纹（u8-l2 讲过的「四连函数」结构相似是天然嫌疑）。xcut 域（[L674-753](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L674-L753)）：RED-10 本地定义遮蔽正牌共享工具（对照 [L72-79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L72-L79) 的 `SHARED_UTILS` 名单：SaturateCast、TensorWrap 等）；RED-11 死代码——static 函数名在整个算子面（priv+绑定）全文只出现一次（即定义处），见 [L719-728](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L719-L728)。

**指南文档即配置**。[tools/refactor_op.py:469-505](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L469-L505) 的 `load_curated` 不读 JSON，而是**解析指南 Markdown 本身**：从 [REFACTOR_OP_GUIDELINES.md:156](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/guidance/REFACTOR_OP_GUIDELINES.md#L156) 抓 `similarity-threshold = 0.80`，从两个代码围栏里抓共享工具名单与按算子的重复豁免清单（`op: 名字, 名字`，目前为空）。调阈值改文档即可，无需动代码——同一份文档既是人的规则又是机器的配置。

**verify 与量化摘要**。[tools/refactor_op.py:820-971](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L820-L971) 的 `check_verify` 逐腿比对；[L974-1057](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L974-L1057) 的 `_api_abi` 对绑定用 binding_api 快照做保守对比；[L1082-1102](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L1082-L1102) 的 `refactor_summary` 从 `git diff --numstat` 算实现/绑定文件的 LOC 增删，并用「base 上打开的 RED 集合 − 工作区仍打开的 RED 集合」量化冗余解决量——每次 verify 报告末尾自动附上 `+x / -y (net)` 与 `redundancy resolved/introduced/still open` 三行，unification 应净减行、introduced 必须为空。

**verdict 三态**。[tools/refactor_op.py:1128-1142](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tools/refactor_op.py#L1128-L1142)：verify 阶段零 GAP 零 MANUAL 是 `PARITY-OK`，只有 MANUAL 是 `NEEDS-LOCAL-PROOF`（等你本地跑完冻结测试与重跑 assess），有 GAP 是 `GATE-FAIL`；assess 恒 `ADVISORY`。

#### 4.4.4 代码实践

**实践目标**：对 Flip 跑 assess，理解指纹算法与豁免机制。

**操作步骤**：

1. 运行：`python3 tools/refactor_op.py Flip`（默认 assess，全部三域）。
2. 若出现 RED-1/RED-6 建议，打开 evidence 指向的两处代码人工比对：它们是不是「Tensor 版与 VarShape 版只差寻址」的孪生体？
3. 做一个阈值敏感性小实验（不修改仓库文件，只改本地副本）：复制 `tools/refactor_op.py` 到 `/tmp` 会因相对路径失效，因此改为直接阅读 `load_curated` 与 `REFACTOR_OP_GUIDELINES.md` 的 Curated data 段，回答：把 `similarity-threshold` 从 0.80 调到 0.60 会导致什么？（只写分析，不实际修改指南。）
4. 运行 `python3 tools/refactor_op.py Flip --phase verify --base main`，在未重构的干净树上观察：VER-1/2/4/5 应为 PASS（零 diff），VER-3 应为 PASS，VER-6/7 为 MANUAL，整体 verdict 为 `NEEDS-LOCAL-PROOF`。

**需要观察的现象**：assess 的退出码恒为 0（即使有建议）；verify 报告末尾的 refactor summary 中 LOC 增删为 0、redundancy resolved/introduced 均为空。

**预期结果**（**待本地验证**）：干净树上 assess 对 Flip 报出的 RED 项取决于 flip.cu 中 Tensor/VarShape 内核的实际相似度；`similarity-threshold` 降到 0.60 会把更多「局部相似但语义不同」的块误报为近似重复（假阳性升高），这也是阈值放在可 review 的指南文档里、默认从紧（0.80）的原因。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_stable_hash` 必须用 blake2b 而不能用 Python 内建 `hash("字符串")`？

**参考答案**：Python 对 str 的内建 hash 默认启用随机化（进程启动时加盐），同一字符串在不同进程里哈希值不同。shingle 指纹跨进程不稳定，意味着两次运行对同一对代码块可能得出不同的相似度甚至不同的 RED-1 列表，报告不再逐字节可复现——「重跑即证明」的完成定义随之失效。blake2b 是确定性密码学哈希，跨进程、跨机器结果一致。

**练习 2**：重构中你顺手把 `TestOpFlip.cpp` 里重复的三行上传样板提炼成了辅助函数，verify 会怎么判？正确的做法是什么？

**参考答案**：VER-1 直接 GAP（测试文件有 diff）——冻结测试面规定重构**永远不许改**算子测试文件，因为测试是被测行为的最终仲裁，动它就有「改尺子量自己」的嫌疑；VER-5 还可能报覆盖矩阵变化。正确做法（指南明示）：把共享助手移到算子测试文件**之外**（如公共测试工具头），或把测试改动拆成独立 MR 单独评审。

**练习 3**：RED-11 判死代码的判据是「static 函数名在整个算子面只出现一次」。给出一个该判据会误报（假阴性/假阳性）的场景。

**参考答案**：假阴性——一个 static 函数 F 只被另一个 static 函数 G 调用，而 G 本身无人调用（成对死代码）：F 出现两次（定义+G 内调用）、G 出现两次，都不满足「只出现一次」，双双漏网。假阳性较少但存在：函数名与同文件里某个注释/字符串中的词撞名时会多计一次出现从而漏报死代码；反过来若调用点全部在条件编译分支中而当前视图没展开宏，也可能误判。所以 RED-11 是 RECOMMENDATION（建议）而非 GAP，删除前仍需人确认——这也解释了为什么死代码检查不给硬失败。

## 5. 综合实践

**任务：给「假想的 Flip 优化战役」写一份合规的开工与验收清单。**

你不真的优化任何内核（那需要 GPU 与完整战役），而是扮演流程工程师，产出一份能被三个工具验证的文档。步骤：

1. **开工侧**：运行 `python3 tools/review_op.py Flip --domain all --format json > /tmp/flip_review.json`，从 JSON 中过滤出全部非 PASS 项，按下表整理成《Flip 战役前置缺口清单》：

   | id | 域 | 状态 | 证据（文件:行） | 现状判断 | 整改动作 | 谁来做（人/CI/agent） |
   |---|---|---|---|---|---|---|

   规则：结构性 MANUAL（SUP-9/TST-5/TST-9/BEN-9/BEN-11/DOC-3/4/6）逐条写人工结论；GAP 按 `fix` 字段转成动作，其中凡涉及基线的动作必须标注「CI 播种，禁止手编」。

2. **门禁侧**：运行 `python3 tools/optimize_op.py Flip --phase preflight`，把 PRE-1..PRE-5 的结论与上表交叉验证：preflight 转述的 review_op finding 是否与你第 1 步的原始输出一致？哪些前置缺口会阻塞开工、哪些只是提醒？

3. **重构侧**：运行 `python3 tools/refactor_op.py Flip` 记录 assess 结果，再挑出最多两条 RED 建议，为每条写出「若应用，verify 需要哪些证明」——至少列出 VER-6 要跑的确切 gtest_filter 命令与 VER-7 的重跑命令。

4. **闭环**：写一段 200 字以内的结论，回答：假设明天你真要优化 flip 的 VarShape 路径，第一个提交应该是什么主题前缀、动哪些文件、不动哪些文件？用黄金法则与 ODO 门的编号支撑你的每个「不动」。

**验收标准**（自评）：清单覆盖全部非 PASS 项；每个基线动作都标注 CI；结论中「不动测试、不动 bench、不动公开头」分别对应 VER-1/VER-2/VER-3 与 ODO-5 的引用正确。

## 6. 本讲小结

- CV-CUDA 用「指南（人读）+ 确定性检查器（机器跑）+ 薄 skill（调度）」三层结构固化算子工程完成标准：REVIEW_OP_GUIDELINES ↔ review_op.py、OPTIMIZATION_GUIDELINES ↔ optimize_op.py、REFACTOR_OP_GUIDELINES ↔ refactor_op.py，完成都定义为「重跑零 GAP 零未解决 MANUAL」。
- 三个工具共享骨架（resolve_op 展开文件地图 → check_* 产出带 `文件:行号` 证据的 Finding → 渲染 md/json → 按各自规则定退出码），但退出码语义不同：review 只看 GAP，optimize 的 evidence 阶段 GAP+MANUAL 都阻塞，refactor 的 assess 永远退出 0（GAP 只在 verify 出现）。
- review_op.py 做**跨表面对账**：解析 C 头 Submit 签名的「主数据输入句柄」与 Doxygen Limitations 表，与 .hpp 重载、Python 绑定、测试覆盖、bench 配置互相对；SUP-10 把「planar 双布局是默认契约」变成机器检查；八个结构性 MANUAL 项把语义判断留给人。
- optimize_op.py 把守优化战役两端：preflight 复用 review_op 的 test/bench 结论并确认基线与画像工具就绪（先基准后改码）；evidence 逐项核对硬证据——逐像素相等、基线更新且无同 key 回归（ODO-7）、API/ABI 未动（ODO-5）、`perf:` 提交卫生（ODO-6）、10 MB 内存门（ODO-9）、单算子范围（ODO-10，OSS 镜像策略缺失即拒绝）、绑定收益过合并标准误（ODO-12）。
- refactor_op.py 用「剥注释 → 括号平衡提块 → 5-gram shingle 的 blake2b 指纹 → Jaccard ≥ 0.80」检测 Tensor/VarShape 孪生内核等语义冗余（assess，建议性），再用 verify 的五条确定性腿（冻结测试/基准面、API/ABI 快照、特征矩阵、覆盖矩阵）+ 两条人工证明（位精确、冗余消除）证明重构「什么都没改变」，并自动量化 LOC 增减与冗余解决量。
- 阈值与豁免清单放在指南文档里被机器回读（refactor 的 similarity-threshold 与 allowlist、review 的 curated JSON），调参走文档评审而非改代码。

## 7. 下一步学习建议

本讲讲义是第八单元最后一篇「工具链」讲义。建议：

1. **向后收尾第九单元**：u9-l1 起进入端到端应用（分类、检测、分割管线），把前八单元的算子、张量、流、缓存知识串成完整 GPU 应用。
2. **想吃透优化门禁的数值基础**：回读 u7-l3（bench/config 的 case-key/SKU/基线结构与 compare_to_baseline.py）——ODO-7/ODO-12 的每一项判断都建立在那套数据模型上。
3. **源码延伸阅读**：
   - [.agents/tools/binding_api.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/tools/binding_api.py)：ODO-5/VER-3 依赖的保守绑定 API 快照如何解析 C++ 声明；
   - [.agents/tools/optimization_summary.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/.agents/tools/optimization_summary.py)：v1 摘要块的生成/刷新/校验三件套，是 `--phase summary` 的实现；
   - `bench/_internal/validate_baselines.py`：ODO-7 实际调用的基线校验器。
4. **动手方向**：找一个你熟悉的小算子（如 Flip）完整走一遍「review 巡检 → 整改结构性 MANUAL → preflight」，把《前置缺口清单》沉淀为自己的工作模板；有 GPU 环境时再尝试 ncu 画像一个 memory-bound 内核，体会「at ridge」判断的依据。
