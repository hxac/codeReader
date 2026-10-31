# 评估启动器 eval_ground3d.sh 与配置族

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出 `eval_ground3d.sh` 的 8 个参数各自的含义与默认值,并理解它如何用「数据集 × QA 层级 × 子任务」三层循环,把一次评估展开成一连串 `tools/test.py` 调用。
2. 解释 `--variant auto` 背后的 LoRA rank 探测机制:为什么 checkpoint 与配置的 LoRA rank 不匹配会导致权重**静默失载**,以及 `probe_lora_rank` 如何用几行 Python 在加载模型之前就规避这个坑。
3. 画出四个评估配置(scannet/scannetpp × joint/_3d)的继承关系图,理解 `_3d` 变体「只覆盖三个字段」的精简设计。
4. 用 `--levels` / `--sub_tasks` 精确控制评估范围,并知道拼错任务名时会发生什么(以及为什么不会报错)。

本讲承接 [u1-l4](u1-l4-quickstart-inference.md)(`tools/test.py` 主线与 `--cfg-options` 点路径)与 [u6-l1](u6-l1-seg-metric.md)(mIoU 指标计算),把视角从「单次推理」拉高到「批量评估的工程组织」。

## 2. 前置知识

### 2.1 Bash 脚本的几种构件

本讲的启动器是一个 167 行的 Bash 脚本,用到四个不太常见的构件,先扫清词汇障碍:

- **`set -euo pipefail`**:让脚本「任何命令失败立即退出、引用未定义变量报错、管道中任一环节失败都算失败」。这是防御性脚本的标配,能尽早暴露错误而不是带病继续。
- **`case ... esac` 与 `while [ $# -gt 0 ]`**:手写的参数解析循环,逐个消费 `--flag value` 形式的参数。相比 `getopts`,这种方式支持长选项名。
- **heredoc(Here Document)**:`python - "$ckpt" <<'PY' ... PY` 表示把 `PY` 标记之间的文本作为**标准输入**喂给 `python -`(`-` 表示从 stdin 读脚本)。引号包裹的 `<<'PY'` 保证文本不做变量展开,Python 代码原样传入,`"$ckpt"` 则通过命令行参数传给 `sys.argv[1]`。
- **关联数组**:`declare -A LEVEL_MAP=( [part]=part_qa_data ... )` 是 Bash 4+ 的字符串键字典,用来把 CLI 简写 `part` 映射成数据管线里的正式键名 `part_qa_data`。

### 2.2 LoRA 参数在 state_dict 里长什么样

[u4-l2](u4-l2-decoder-init-and-lora.md) 已经讲过 LoRA 的原理,这里只回顾与权重命名相关的部分。LoRA 把一次权重更新分解为两个低秩矩阵的乘积:

\[
W' = W + \frac{\alpha}{r}\, B A,\qquad A\in\mathbb{R}^{r\times d_{\text{in}}},\; B\in\mathbb{R}^{d_{\text{out}}\times r}
\]

其中 \(r\) 就是 **LoRA rank**。经过 `peft` 库的 `get_peft_model` 注入后,每个被适配的线性层都会多出名为 `...lora_A.weight` 与 `...lora_B.weight` 的参数。关键观察:`lora_A` 对应的矩阵形状是 \((r, d_{\text{in}})\),所以 **`lora_A.weight` 的第 0 维就是 rank \(r\)**——这正是 rank 探测的依据。

### 2.3 「静默失载」为什么可怕

mmengine 的 Runner 加载 checkpoint 时默认是非严格模式(`strict=False`):checkpoint 里找不到的键、或形状对不上的键,只会打一条警告甚至完全不打,**不会抛异常**。正常情况下这是宽容;但如果配置写的是 `lora_r=16` 而权重是按 `r=32` 训练的,所有 `lora_A/lora_B` 的形状都对不上,全部被静默丢弃——模型无声地退回**基座 Qwen**,推理照常运行、不报任何错,只是指标大幅下降。这类 bug 的可怕之处在于:它看起来像「模型变差了」,而不是「加载失败了」。仓库的配置 docstring 原文就点明了这一点(见 4.2.3)。

### 2.4 回顾:三层任务旋钮与点路径

[u1-l4](u1-l4-quickstart-inference.md) 建立过两个事实,本讲反复用到:

- `tools/test.py` 中 checkpoint 是**位置参数**,且会无条件覆盖配置里的 `load_from`([tools/test.py:L19](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L19)、[tools/test.py:L131](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L131));`--cfg-options` 经 `cfg.merge_from_dict` 深合并进配置([tools/test.py:L119-L120](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L119-L120))。
- 评估哪个任务由 `test_pipeline` 里 `QA_Generation_Test` 的 `task_level` / `task_type` / `sub_task` 三个旋钮决定,点路径为 `test_dataloader.dataset.pipeline.2.transforms.2`(两个下标分别指向 `MultiScaleFlipAug3D` 与它内部的这个变换)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [eval_ground3d.sh](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh) | 统一评估启动器:解析参数、探测变体、三层循环批量调用 `tools/test.py` |
| [configs/ground3dlmm_eval_ground3d_scannet.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py) | ScanNet × Joint(3D+2D)完整评估配置,是整个配置族的根 |
| [configs/ground3dlmm_eval_ground3d_scannet_3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py) | ScanNet × 3D-only,继承上面的配置,只覆盖 3 个字段 |
| [configs/ground3dlmm_eval_ground3d_scannetpp.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannetpp.py) | ScanNet++ × Joint,数据根与图像目录换成 scannetpp |
| [configs/ground3dlmm_eval_ground3d_scannetpp_3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannetpp_3d.py) | ScanNet++ × 3D-only,同样只覆盖 3 个字段 |
| [uniseg3d/transforms_3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py) | `QA_Generation_Test` 的三级过滤实现(4.4 节) |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | LoRA 注入处,决定 state_dict 中 `lora_A` 键的形状(4.2 节) |
| [tools/part3d_glamm_data_utils.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py) | `ALL_TASKS` 常量:9 个合法子任务名的权威清单 |
| [tools/test.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py) | 真正干活的测试入口(启动器只是它的「批量遥控器」) |

## 4. 核心概念与源码讲解

### 4.1 模块一:eval_ground3d.sh 参数体系与三层循环

#### 4.1.1 概念说明

一个完整的 Ground3D 评估 = **若干次独立的 `tools/test.py` 运行**。为什么要拆成多次?因为[u6-l1](u6-l1-seg-metric.md)讲过,mIoU 是按「QA 条目」聚合的,而不同子任务、不同层级(part/object/multi_turn)的样本量与难度差异巨大,论文需要**分任务报告**指标。把「数据集 × QA 层级 × 子任务」的每个组合单独跑一次,每次的预测 JSON 与 mIoU 自然就按目录分好类了。

`eval_ground3d.sh` 做的事就是:**把这三个维度的笛卡尔积展开成一串 `tools/test.py` 命令,并给每次运行分配互不冲突的输出目录**。理解了这一点,整个脚本就只是「参数解析 + 循环 + 拼命令」三段式。

#### 4.1.2 核心流程

```text
解析 8 个 CLI 参数(带默认值)
        │
        ▼
校验 --variant ∈ {joint, 3d, auto}
        │
        ▼  (variant=auto 时)
probe_lora_rank(checkpoint) ──► rank=16 → joint / rank=32 → 3d
        │                        无 LoRA → 默认 joint(告警)
        │                        多种 rank 混杂 → 报错退出
        ▼
CONFIG_SUFFIX = "_3d" 或 ""        # 决定用哪个配置文件
OUT_ROOT      = val_outputs_$变体   # joint 与 3d 的输出永不互相覆盖
        │
        ▼
for 数据集 in {scannet, scannetpp}:          ← --dataset
  for 层级 in {part, object, multi_turn}:     ← --levels
    if multi_turn: 只覆盖 task_level(一整段对话不分子任务)
    else: for 子任务 in 9 个子任务:            ← --sub_tasks
            覆盖 task_level + task_type=qa_data + sub_task
            run_test(配置, 唯一的 save_dir, 覆盖项...)
```

#### 4.1.3 源码精读

**默认值与参数解析。** 8 个参数及其默认值集中声明在一处,脚本头部注释(L1-L25)就是帮助文档——`--help` 的实现方式是直接 `grep '^#' "$0"` 把注释行抽出来打印:

[eval_ground3d.sh:L28-L35](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L28-L35) 声明默认值:数据集默认 `scannet`,层级默认 `part,object`,子任务默认全部,checkpoint 默认 `work_dirs/ground3dlmm/pytorch_model.pth`,变体默认 `auto`。

[eval_ground3d.sh:L37-L50](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L37-L50) 是 `while/case` 参数循环:每个 `--flag` 消费两个位置(`shift 2`),未知选项直接报错退出;第 47 行 `-h|--help` 分支用 `grep '^#' "$0" | sed 's/^#\s\?//'` 把脚本自己的头注释变成帮助文本——**一份注释两用**,不用维护两份文档。

**三层循环。** 这是脚本的主体:

[eval_ground3d.sh:L145-L166](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L145-L166) 外层按数据集选配置文件名(`configs/ground3dlmm_eval_ground3d_${ds}${CONFIG_SUFFIX}.py`),中层把 CLI 简写经关联数组 `LEVEL_MAP` 翻译成正式键名,内层对 part/object 层级再按子任务逐个发起调用,而 **multi_turn 是特例**——它只设 `task_level`,不设 `task_type/sub_task`:

[eval_ground3d.sh:L152-L155](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L152-L155) 多轮对话整段评估,输出目录只到 `.../multi_turn_qa_data` 这一级。

[eval_ground3d.sh:L156-L163](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L156-L163) part/object 则对每个子任务单独跑一次,每次都注入三个点路径覆盖:`task_level`、`task_type=qa_data`、`sub_task=$sub`,输出目录多出 `qa_data/$sub` 两级——**目录树本身就是「预测按任务归档」的物化**。

**run_test:一次调用的完整形态。**

[eval_ground3d.sh:L128-L143](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L128-L143) 定义了单次运行:预留了 `DRYRUN` 环境变量干跑开关(只打印不执行,4.1.4 的实践会用到);用一个 `while true` 循环随机生成 10000-59151 范围的端口并用 `nc -z` 探测,直到找到空闲端口,避免连续多次启动时 `master_port` 冲突;最后通过 `env PYTHONPATH=...` 起 `torch.distributed.launch` 运行 `tools/test.py`,把 checkpoint 作为位置参数传入,并通过 `--cfg-options` 注入 `model.decoder.save_pred_qa_dir=$save_dir`([u4-l5](u4-l5-infer-reason-eval.md)讲过,这个开关决定预测 JSON 写到哪)与外层透传的 `$@` 任务覆盖项。

还有一个精巧细节——**mask dump 的镜像目录**:

[eval_ground3d.sh:L136-L137](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L136-L137) 若指定了 `--mask-dump`,则设置环境变量 `MASK_DUMP_DIR=<dump根>/<save_dir去掉OUT_ROOT前缀的子路径>`,让每个子任务的掩码落盘目录与预测 JSON 目录**同构**,这样后续 GM-δ 文本指标阶段([u6-l3](u6-l3-text-metrics.md))能把两者一一对齐。`${save_dir#"$OUT_ROOT/"}` 是 Bash 的「删除最短前缀」语法。

**GPU 数自动探测。**

[eval_ground3d.sh:L123-L125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L123-L125) 未显式给 `--gpus` 时,按 `CUDA_VISIBLE_DEVICES` 里逗号分隔的设备数确定进程数,否则退化为单卡。

#### 4.1.4 代码实践:零 GPU 干跑,数清命令矩阵

**实践目标**:不消耗任何 GPU 与模型权重,验证你对三层循环的理解——给定参数组合,脚本到底会发出几次 `tools/test.py` 调用、每次的输出目录是什么。

**操作步骤**:

1. 在仓库根目录执行(利用脚本内置的 `DRYRUN` 开关):

   ```bash
   DRYRUN=1 bash eval_ground3d.sh --dataset scannet --levels part,object \
       --sub_tasks distance_estimation,scale_estimation
   ```

2. 再跑一个全量组合观察规模:

   ```bash
   DRYRUN=1 bash eval_ground3d.sh --dataset all --levels all
   ```

3. 对照输出,数一数每次组合展开出多少条 `[dry-run]` 行。

**需要观察的现象**:第一条命令应打印 2(层级)× 2(子任务)= 4 条干跑行,每行含 `config=...`、`save_dir=...`、`gpus=...`;第二条命令是 2(数据集)× (2 × 9 + 1)(part/object 各 9 个子任务 + multi_turn 单独一次)= 38 条。同时注意所有行的 `save_dir` 互不相同,且默认根目录形如 `val_outputs_joint/` 或 `val_outputs_3d/`(取决于变体探测结果;没有 checkpoint 时 auto 会直接报错,可加 `--variant joint` 让干跑继续)。

**预期结果**:干跑行数与手工推算的笛卡尔积一致;`save_dir` 目录树与 [eval_ground3d.sh:L147](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L147) 与 L154/L159 的拼接逻辑一致。若行为与此不符,以你本地的实际输出为准(待本地验证)。

#### 4.1.5 小练习与答案

**练习 1**:命令 `bash eval_ground3d.sh --dataset scannetpp --levels multi_turn --variant 3d` 会调用 `tools/test.py` 几次?分别用什么配置文件?

**答案**:1 次。multi_turn 层级不分子任务,只发起一次调用([eval_ground3d.sh:L152-L155](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L152-L155));数据集是 scannetpp、变体是 3d,故配置文件为 `configs/ground3dlmm_eval_ground3d_scannetpp_3d.py`(由 L146 的 `${ds}${CONFIG_SUFFIX}` 拼出,`CONFIG_SUFFIX="_3d"` 来自 L106)。

**练习 2**:为什么 `run_test` 里要专门探测一个空闲端口,而不是固定写死一个 `master_port`?

**答案**:因为一次完整评估会**连续发起几十次** `torch.distributed.launch`(4.1.4 的 38 次就是一例)。若端口写死,前一次进程尚未完全释放端口、或两次评估并行时,后一次会因端口占用而握手失败。[eval_ground3d.sh:L134-L135](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L134-L135) 用随机端口 + `nc -z` 探测循环,每次都找一个当前空闲的端口。

**练习 3**:不指定 `--out-root` 时,为什么默认输出根目录要带上变体名(`val_outputs_joint` / `val_outputs_3d`)?

**答案**:见 [eval_ground3d.sh:L107-L109](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L107-L109) 的注释:防止先跑 joint 再跑 3d(或反过来)时,后一次的预测 JSON **覆盖**前一次——两个变体的输出目录树完全同构,若共用一个根目录就会互相冲掉。

### 4.2 模块二:LoRA rank 探测与变体自动选择

#### 4.2.1 概念说明

项目发布了两个权重相当的变体(README 模型动物园):

| 变体 | 输入 | LoRA r / α | 对应配置 |
| --- | --- | --- | --- |
| Ground3D-LMM-4B-Joint | 3D 点云 + RGB 帧 | 16 / 32 | `..._scannet.py` / `..._scannetpp.py` |
| Ground3D-LMM-4B-3D | 仅 3D 点云 | 32 / 64 | `..._scannet_3d.py` / `..._scannetpp_3d.py` |

见 [README.md:L206-L207](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L206-L207)。两个变体的差异会一路「烧进」checkpoint:Joint 用 r=16 训练,3D-only 用 r=32 训练。若配置与 checkpoint 的 rank 不一致,如 2.3 节所述,所有 LoRA 权重形状对不上、被 `strict=False` 静默丢弃,模型退回基座 Qwen——**不报错,只掉点**。

启动器的解法朴素而有效:在加载模型**之前**,先用几行 Python 打开 checkpoint 文件,统计所有 `lora_A` 权重的第 0 维(即 rank),据此自动选择匹配的配置。这就是 `--variant auto`(默认值)。

#### 4.2.2 核心流程

```text
checkpoint 文件 (.pth)
   │ torch.load(cpu)           # 兼容无 weights_only 参数的旧版 torch
   ▼
state_dict                   # 兼容 mmengine 包装格式 {'state_dict': ...}
   │ 扫描所有含 'lora_A' 的键
   ▼
ranks = {每个 lora_A.weight.shape[0]}
   │
   ├─ ranks 为空        → "none"   → 默认 joint(告警)
   ├─ |ranks| == 1      → 该 rank  → 16→joint,32→3d,其他→报错
   └─ |ranks| > 1       → "mixed"  → 报错退出,要求用户显式指定
```

为什么 `lora_A.weight.shape[0]` 恰好是 rank?回看 2.2 节的分解:\(A\in\mathbb{R}^{r\times d_{\text{in}}}\) 是 peft 里的 `lora_A` 线性层(`nn.Linear(d_in, r)`)的权重,PyTorch 线性层权重按 \((\text{out}, \text{in})\) 存储,故 shape[0] = \(r\)。

#### 4.2.3 源码精读

**问题陈述(脚本作者亲自写的注释)。**

[eval_ground3d.sh:L56-L60](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L56-L60) 注释明确解释了动机:joint checkpoint 训练于 r=16、point-only 训练于 r=32,「把 checkpoint 加载进 rank 错误的配置会让 adapters 被静默丢弃(strict=False),推理退回基座 Qwen」,所以要探测 rank、选对配置。

**probe_lora_rank 函数本体。**

[eval_ground3d.sh:L61-L82](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L61-L82) 定义探测函数:Bash 侧只是个壳,把 checkpoint 路径传给内嵌的 Python heredoc。Python 侧([L63-L81](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L63-L81))做四件事:

1. `torch.load(p, map_location='cpu')` 把整个权重字典加载到 CPU(不碰 GPU、不构建模型),并用 `try/except TypeError` 兼容没有 `weights_only` 参数的旧版 PyTorch;
2. 若是 mmengine 包装格式(`'state_dict' in sd`)则剥出内层;
3. 收集所有键名含 `lora_A` 且形状非空的权重的 `shape[0]` 到集合 `ranks`;
4. 按集合大小输出:`none`(没有 LoRA)、`mixed:r1,r2`(多种 rank 并存)、或唯一的 rank 值。

注意它用**子串匹配** `'lora_A' in k` 而非精确键名,这样无论 peft 版本把模块命名成 `...model.layers.0.self_attn.q_proj.lora_A.weight` 还是别的形式,都能命中。

**决策分支。**

[eval_ground3d.sh:L84-L105](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L84-L105) 只有 `variant=auto` 才探测:checkpoint 文件不存在直接报错([L85-L89](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L85-L89));随后按 rank 映射:16→joint、32→3d([L93-L94](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L93-L94));找不到任何 LoRA 时**默认 joint 并打告警**([L95-L97](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L95-L97));rank 混杂或无法识别则报错退出,要求用户显式传 `--variant`([L98-L104](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L98-L104))——「拿不准就让人来定」,不猜。

**变体 → 配置后缀。**

[eval_ground3d.sh:L106](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L106) 一行完成映射:`3d` → 后缀 `_3d`,`joint` → 空后缀。这就是 2×2 配置矩阵的「变体维」。

**rank 不匹配为何必然失载(配置侧证据)。** LoRA 的 rank 来自配置的 `lora_r` 字段,在解码器初始化时进入 `LoraConfig`:

[uniseg3d/query_decoder.py:L1000-L1008](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1000-L1008) 用配置中的 `r=lora_r, lora_alpha=lora_alpha` 构造 `LoraConfig` 并经 `get_peft_model` 注入模型——rank 在**建模型时**就已决定了每个 `lora_A` 的形状;之后 Runner 以 `strict=False` 加载 checkpoint 时,形状不一致的键全部被丢弃。配置 docstring 对此有一手说明:

[configs/ground3dlmm_eval_ground3d_scannet_3d.py:L1-L17](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py#L1-L17) 文件头 docstring 完整解释了三字段覆盖的动机,原文明确写道「loading the point-only weights into an r=16 model produces mismatched adapters that get silently ignored by `strict=False`, so the model silently falls back to base Qwen」。

**注意一个边界**:若用户**显式**传了 `--variant joint`(非 auto),脚本不再探测、完全信任用户([L84](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L84) 的 `if [ "$VARIANT" = auto ]` 只在 auto 分支探测)。所以显式指定变体时,配错 checkpoint 依然会静默失载——auto 是唯一带保护的模式。

#### 4.2.4 代码实践:手工复现 rank 探测,造出四种结局

**实践目标**:不依赖任何真实 checkpoint,用一个小脚本在 CPU 上构造四种典型的 state_dict,验证 probe 逻辑的每个分支,从而确信「rank 探测 = 扫 `lora_A` 的 shape[0]」。

**操作步骤**(示例代码,可在任意装有 torch 的环境运行):

```python
# probe_demo.py —— 示例代码(非项目原有文件),复现 eval_ground3d.sh 的 rank 探测逻辑
import torch

def probe(sd):  # 与 eval_ground3d.sh L72-L80 等价
    ranks = {int(v.shape[0]) for k, v in sd.items()
             if 'lora_A' in k and hasattr(v, 'shape') and v.ndim >= 1}
    if not ranks:
        return "none"
    if len(ranks) > 1:
        return "mixed:" + ",".join(str(r) for r in sorted(ranks))
    return str(next(iter(ranks)))

# ① joint 风格:所有 lora_A 形状 [16, d]
sd16 = {'model.layers.0.self_attn.q_proj.lora_A.weight': torch.zeros(16, 256),
        'model.layers.0.self_attn.q_proj.lora_B.weight': torch.zeros(256, 16)}
# ② 3d 风格:r=32
sd32 = {'model.layers.0.self_attn.q_proj.lora_A.weight': torch.zeros(32, 256)}
# ③ 没有 LoRA(例如误传了基座 Qwen 权重)
sd_none = {'model.layers.0.self_attn.q_proj.weight': torch.zeros(256, 256)}
# ④ rank 混杂(理论上不应出现,防御性分支)
sd_mixed = {**sd16, 'model.layers.1.self_attn.q_proj.lora_A.weight': torch.zeros(32, 256)}

for name, sd in [('joint', sd16), ('3d', sd32), ('none', sd_none), ('mixed', sd_mixed)]:
    print(f"{name:6s} -> {probe(sd)}")
```

**需要观察的现象**:输出依次为 `joint -> 16`、`3d -> 32`、`none -> none`、`mixed -> 16,32`;对照 [eval_ground3d.sh:L92-L104](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L92-L104),前两者分别映射到 joint / 3d,后两者分别走「默认 joint + 告警」与「报错退出」分支。

**预期结果**:如上四行输出。另外注意 `lora_B` 键虽然存在,但不参与判定——它的形状是 \((d_{\text{out}}, r)\),第 0 维不是 rank,所以探测只认 `lora_A`。真实 checkpoint 的精确键名与 peft 版本有关,以实际打印为准(待本地验证)。

#### 4.2.5 小练习与答案

**练习 1**:为什么探测函数匹配 `lora_A` 而不是 `lora_B`?两者都能推出 rank 吗?

**答案**:都能推出(`lora_B.weight` 的**第 1 维**是 rank),但 `lora_A.weight` 的**第 0 维**即 rank,取 `shape[0]` 更直接,且不需要再取 `shape[1]`。更重要的是:只要两个矩阵有一个被注入,就必然同时存在(`get_peft_model` 成对注入,见 [uniseg3d/query_decoder.py:L1008](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1008)),匹配任意一个即可,选写法最短的那个。

**练习 2**:假设你微调了一个 r=8 的自定义 checkpoint,用 `--variant auto` 评估会发生什么?正确的评估命令是什么?

**答案**:rank=8 不在 {16, 32} 里,脚本走 [eval_ground3d.sh:L101-L103](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L101-L103) 的兜底分支,打印「unrecognized LoRA rank (8)」并退出。auto 只认两个官方发布 rank。正确做法:复制一份 `_3d` 配置(它演示了最小改动模板),把 `lora_r`/`lora_alpha` 改成 8 与你的 α、按需设 `image_dir`,再显式用该配置跑 `tools/test.py`(或临时改脚本 rank 映射)。

**练习 3**:探测不到任何 LoRA 键时(`none`),脚本为什么默认 joint 而不是报错?

**答案**:见 [eval_ground3d.sh:L95-L97](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L95-L97):这种情况多半是用户传错了文件(比如传了基座 Qwen 的原始权重,或 SD 格式不符合预期),但也不能排除「确实想评估无 LoRA 基线」的合法场景。作者选择打一条 stderr 告警后按 joint 走——最坏情况也只是评估结果不好,比把合法场景一刀切报错更稳妥。

### 4.3 模块三:配置继承体系——2×2 矩阵与「只改三字段」

#### 4.3.1 概念说明

四个评估配置构成一个 2×2 矩阵:**数据集维**(scannet / scannetpp)× **变体维**(joint / _3d)。[u1-l3](u1-l3-repo-structure-and-registry.md) 讲过 mmengine 的配置继承与递归覆盖,这里是最教科书式的应用:

- `ground3dlmm_eval_ground3d_scannet.py` 是**根配置**,joint × scannet,内容完整(模型、管线、数据、运行时一应俱全);
- scannetpp 版是「换数据根」的**平行完整配置**(它没有继承 scannet 版,而是同样从 mmdet3d 的两个 base 继承);
- 两个 `_3d` 版各自继承对应的 joint 版,**只覆盖 3 个字段**。

为什么 `_3d` 不复制整份配置?因为两个变体的差异被精确约束在解码器构造参数上——复制整份配置意味着未来任何改动都要同步两处,必然漂移。三字段覆盖让「变体差异」在文件里**一眼可见、不可发散**。

#### 4.3.2 核心流程

```text
mmdet3d::default_runtime.py ┐
mmdet3d::scannet-seg.py     ┴─► ground3dlmm_eval_ground3d_scannet.py      (joint × scannet,根)
                                   │ _base_ 继承 + 3 字段覆盖
                                   └─► ground3dlmm_eval_ground3d_scannet_3d.py   (3d × scannet)

mmdet3d::default_runtime.py ┐
mmdet3d::scannet-seg.py     ┴─► ground3dlmm_eval_ground3d_scannetpp.py    (joint × scannetpp)
                                   │ _base_ 继承 + 3 字段覆盖
                                   └─► ground3dlmm_eval_ground3d_scannetpp_3d.py (3d × scannetpp)

启动器拼名: configs/ground3dlmm_eval_ground3d_${数据集}${变体后缀}.py
```

#### 4.3.3 源码精读

**根配置的关键字段(joint × scannet)。**

[configs/ground3dlmm_eval_ground3d_scannet.py:L44-L74](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L44-L74) 声明 `Grounded_Decoder_Eval` 解码器,其中与本讲直接相关的字段:`lora_r=16, lora_alpha=32`(L68-L69,Joint 变体的 rank)、`image_dir='data/scannet/frame_images'`(L74,[u4-l6](u4-l6-image-branch-joint-vs-3d.md) 讲过解码器按场景名在此目录找 RGB 帧)。

[configs/ground3dlmm_eval_ground3d_scannet.py:L104-L110](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L104-L110) `data_root = 'data/scannet'` 与四键 `data_prefix`——[u2-l3](u2-l3-dataset-and-loading.md) 的「裸文件名契约」在此生效。

[configs/ground3dlmm_eval_ground3d_scannet.py:L195-L198](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L195-L198) `QA_Generation_Test` 的三级旋钮默认值为 `task_level='multi_turn_qa_data', task_type=None, sub_task=None`——**裸跑 `tools/test.py` 不加覆盖时,只评估多轮对话这一层**。启动器的每个循环体都会显式覆盖 `task_level`(以及 part/object 时的另两个旋钮),所以批量评估不受这个默认值影响,但手工单跑时要意识到它。

[configs/ground3dlmm_eval_ground3d_scannet.py:L242-L246](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L242-L246) 评估器为 `Part3DGlamm_SegMetric`([u6-l1](u6-l1-seg-metric.md) 的主角),`test_dataloader = val_dataloader`(L238)复用同一份测试管线。

**scannetpp 版:数据集维的平行配置。**

[configs/ground3dlmm_eval_ground3d_scannetpp.py:L104](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannetpp.py#L104) `data_root = 'data/scannetpp'`,[L74](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannetpp.py#L74) `image_dir='data/scannetpp/frame_images'`,其余(模型结构、rank=16、管线、旋钮默认值)与 scannet 版逐字段相同。得益于「裸文件名契约」,两个数据集**共用同一套配置结构**,只换根目录。

**_3d 版:变体维的最小覆盖。**

[configs/ground3dlmm_eval_ground3d_scannet_3d.py:L19-L27](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py#L19-L27) 全部实质内容:`_base_` 指向同名无后缀配置,`model.decoder` 里仅覆盖 `lora_r=32`、`lora_alpha=64`、`image_dir=None` 三项。mmengine 的递归合并会把这些字段「深写」进继承来的嵌套 dict,其余全部原样保留。

[configs/ground3dlmm_eval_ground3d_scannetpp_3d.py:L19-L27](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannetpp_3d.py#L19-L27) scannetpp 的 `_3d` 版结构完全相同,`_base_` 指向 scannetpp 的 joint 版——两个 `_3d` 文件除了 `_base_` 路径外逐行一致,各自 27 行(含 docstring)。

三个覆盖字段各司其职:

| 字段 | joint 值 | _3d 值 | 作用 |
| --- | --- | --- | --- |
| `lora_r` | 16 | 32 | 与 checkpoint 的 LoRA rank 对齐,否则静默失载 |
| `lora_alpha` | 32 | 64 | 缩放系数 α,与训练时的比例一致(α = 2r) |
| `image_dir` | `data/<ds>/frame_images` | `None` | [u4-l6](u4-l6-image-branch-joint-vs-3d.md):`None` 时 message 里的 image 条目被过滤,序列零视觉 token,无需改任何解码代码 |

#### 4.3.4 代码实践:用 mmengine 验证「只差三字段」

**实践目标**:用 `mmengine.Config` 实际加载两个变体的配置并做 diff,亲眼确认 `_3d` 继承之后**有且仅有**三个字段的取值不同,其余(包括三层旋钮默认值、data_root、评估器)完全一致。

**操作步骤**(示例代码):

```bash
# 示例代码:在仓库根目录运行(需要已按 u1-l2 装好环境)
python -c "
from mmengine.config import Config
a = Config.fromfile('configs/ground3dlmm_eval_ground3d_scannet.py')
b = Config.fromfile('configs/ground3dlmm_eval_ground3d_scannet_3d.py')
print('data_root :', a.data_root, '|', b.data_root)
for k in ('lora_r', 'lora_alpha', 'image_dir'):
    print(f'decoder.{k:10s}:', a.model.decoder[k], '|', b.model.decoder[k])
print('task_level default:', b.test_dataloader.dataset.pipeline[2].transforms[2].task_level)
"
```

**需要观察的现象**:`data_root` 两边都是 `data/scannet`(继承保留);三个 LoRA/图像字段分别为 `16 | 32`、`32 | 64`、`data/scannet/frame_images | None`;而 `_3d` 配置的 `test_pipeline` 里 `QA_Generation_Test` 的默认 `task_level` 仍是被继承下来的 `'multi_turn_qa_data'`——证明覆盖是外科手术式的,没有波及管线。

**预期结果**:如上所述。若想进一步穷举差异,可把两份配置 `to_dict()` 后递归比较键值,完整的差异清单应恰好是那三个字段(待本地验证)。

#### 4.3.5 小练习与答案

**练习 1**:`ground3dlmm_eval_ground3d_scannetpp.py` 为什么不继承 `ground3dlmm_eval_ground3d_scannet.py`,而是回到 mmdet3d 的 base 重新写一份?

**答案**:观察 [configs/ground3dlmm_eval_ground3d_scannetpp.py:L1-L4](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannetpp.py#L1-L4):它的 `_base_` 与 scannet 版完全相同。若让它继承 scannet 版再覆盖 `data_root/image_dir`,理论上也可行,但两份配置本就是「同一模型在两个数据集上的平行实例」,互相继承反而制造「scannet 是模板、scannetpp 是衍生物」的假层级;此外 scannetpp 版还携带自己的优化器等训练遗留字段(对评估无影响)。选择平行而非链式继承,是让每个数据集配置独立可读、可单独演化。真正适合继承的是「同数据集、仅模型变体不同」的 `_3d` 关系。

**练习 2**:如果把 `_3d` 配置里的 `lora_alpha=64` 误删(只留 `lora_r=32`),推理会发生什么?

**答案**:`lora_alpha` 回落到继承值 32。此时 LoRA 权重**能正常加载**(rank 匹配,键的形状一致,`strict=False` 不会丢任何东西),但每个 adapter 的有效缩放从训练时的 \(\alpha/r = 64/32 = 2\) 变成 \(32/32 = 1\),所有 adapter 的输出减半。这是比「静默失载」更隐蔽的坑:加载日志干净、模型确实是微调版,只是推理质量悄悄打折。这正是 `_3d` 配置 docstring 强调三字段成对覆盖的原因。

**练习 3**:启动器如何仅凭 `--dataset` 和 `--variant` 两个参数定位到 2×2 矩阵中的唯一配置?

**答案**:[eval_ground3d.sh:L146](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L146) 用字符串拼接 `configs/ground3dlmm_eval_ground3d_${ds}${CONFIG_SUFFIX}.py`:`ds` 来自 `--dataset`(scannet/scannetpp),`CONFIG_SUFFIX` 在 [L106](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L106) 由变体决定(`3d` → `_3d`,否则空)。四个组合恰好对应四个文件的命名约定——**文件名即坐标系**。

### 4.4 模块四:--levels / --sub_tasks 的落点——QA_Generation_Test 三级过滤

#### 4.4.1 概念说明

启动器把 `--levels part` 翻译成 `task_level=part_qa_data`、把 `--sub_tasks scale_estimation` 翻译成 `sub_task=scale_estimation` 之后,这些字符串去哪了?答案是测试管线中的 `QA_Generation_Test` 变换:它在**每个场景加载时**遍历该场景的 QA 字典,按三级键逐层过滤,决定「这个场景要回答哪些问题」。

理解这个过滤机制最重要的两点:

1. **过滤是严格匹配**(键必须逐字符相等),不是模糊包含;
2. **匹配不到不报错**——目标集合为空时,该场景一条 QA 都不产生,评估照常运行、指标空空如也。拼错任务名 = 白跑一遍 GPU。

#### 4.4.2 核心流程

QA 数据的嵌套结构(承接 [u2-l1](u2-l1-dataset-structure.md)/[u2-l2](u2-l2-data-conversion.md)):

```text
qa_data                          ← 数据集根
├── part_qa_data                 ← 第一级:task_level(--levels part)
│   └── qa_data                  ← 第二级:task_type(启动器固定传 qa_data)
│       ├── functional_part_grounding   ← 第三级:sub_task(--sub_tasks)
│       │   └── [ {question, answer}, ... ]
│       ├── distance_estimation
│       └── ...共 9 个任务名(ALL_TASKS)
├── object_qa_data
└── multi_turn_qa_data           ← 特例:值为「对话(轮次列表)的列表」,不走二三级
```

过滤伪代码:

```text
for level in (task_level 若给出且存在,否则全部):
    if level == multi_turn: 整段对话直接收集,打上 multi_turn 标签
    else:
        for key in (task_type 若给出且存在,否则全部):
            for sub in (sub_task 若给出且存在,否则全部):
                收集 qa_data[level][key][sub],给每条打 qa_keys/qa_type 标签
任一层「给出但不存在」→ 该层目标集合为空 → 该层零产出(无报错)
```

#### 4.4.3 源码精读

**三级旋钮的声明。**

[uniseg3d/transforms_3d.py:L550-L556](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L550-L556) `QA_Generation_Test.__init__` 接收 `task_level`(注释列出合法值为三个 `*_qa_data`)、`task_type`、`sub_task`,均可为 `None`(表示该层不过滤)或列表(多选)。

**第一级:task_level。**

[uniseg3d/transforms_3d.py:L563-L572](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L563-L572) 给定 `task_level` 时严格查字典:存在则只看这一层,不存在则 `target_levels = []`——**注意这个空列表不会触发任何告警**,直接零产出。这解释了为什么 `--levels` 拼错(比如 `parts`)时脚本反而会在更早的 `LEVEL_MAP` 查表处报错([eval_ground3d.sh:L150-L151](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L150-L151) 对非法层级条目显式报错退出),而 `--sub_tasks` 拼错却静默——前者在 Bash 侧有白名单校验,后者没有。

**多轮特例。**

[uniseg3d/transforms_3d.py:L574-L586](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L574-L586) `multi_turn_qa_data` 的值不是「任务名 → QA 列表」而是「对话列表」,每段对话是轮次字典的列表;此处给每轮打上 `qa_keys='multi_turn'` 标签并保持对话完整——这呼应启动器对 multi_turn 不设 `task_type/sub_task` 的设计:多轮没有子任务维度,整段一起评。

**第二、三级:task_type 与 sub_task。**

[uniseg3d/transforms_3d.py:L592-L604](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L592-L604) 与 [L606-L619](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L606-L619) 两段过滤逻辑同构:给出则严格匹配(支持单值或列表),给出但不存在 → 空集,同样静默。启动器对 part/object 固定传 `task_type=qa_data`([eval_ground3d.sh:L161](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L161)),因为 Ground3D 转换链产出的单轮 QA 都收纳在 `qa_data` 这一键下。

**合法子任务名的权威清单。**

[tools/part3d_glamm_data_utils.py:L34-L39](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L34-L39) `ALL_TASKS` 常量列出 9 个任务名。启动器侧的清单:

[eval_ground3d.sh:L111-L113](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L111-L113) `ALL_SUB_TASKS` 与 `ALL_TASKS` **逐名一致、顺序相同**——两处清单必须保持同步,这是又一个隐含契约(改一处不改另一处,新增任务就会「转换侧有、启动器跑不到」)。

**落点的最后一块拼图**:启动器拼出的点路径 `test_dataloader.dataset.pipeline.2.transforms.2.task_level=...` 中,`pipeline.2` 是 `test_pipeline` 的第 3 个变换(`MultiScaleFlipAug3D`),其 `transforms.2` 是内部第 3 个变换(恰为 `QA_Generation_Test`,见 [configs/ground3dlmm_eval_ground3d_scannet.py:L181-L201](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L181-L201) 的列表顺序)——在 u1-l4 已建立,这里由启动器的每个循环体([eval_ground3d.sh:L160-L162](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L160-L162))实际使用。

#### 4.4.4 代码实践:拼错任务名,观察「静默空转」

**实践目标**:亲眼看一次「严格过滤匹配不到 → 零产出且无报错」的行为,建立对这类静默失败的手感,并学会用数据侧清单交叉校验 `--sub_tasks`。

**操作步骤**:

1. 先做正确性与拼写的交叉校验(零成本):

   ```bash
   # 数据侧权威清单(Ground3D 9 个任务名)
   grep -A6 '^ALL_TASKS' tools/part3d_glamm_data_utils.py
   # 启动器侧清单,应当与上面完全一致
   grep -A3 'ALL_SUB_TASKS=' eval_ground3d.sh
   ```

2. 若本地已有转换好的数据(见 [u2-l2](u2-l2-data-conversion.md)),用一段最小 Python 直接调用过滤函数,分别传入正确与拼错的子任务名(示例代码):

   ```python
   # sub_task_filter_demo.py —— 示例代码:直接驱动 QA_Generation_Test 的过滤逻辑
   from uniseg3d.transforms_3d import QA_Generation_Test

   qa_data = {                       # 模拟一个场景的最小 qa_data(u2-l2 转换产物的形状)
       'part_qa_data': {'qa_data': {
           'distance_estimation': [{'question': 'How far is the chair?<SEG>', 'answer': 'about 1.2 m'}],
       }},
   }

   for sub in ('distance_estimation', 'distance_estimatoin'):   # 第二个是拼写错误
       t = QA_Generation_Test(task_level='part_qa_data', task_type='qa_data', sub_task=sub)
       print(f"sub_task={sub!r:28s} -> {len(t.flatten_qa_data(qa_data))} 条 QA")
   ```

   注意 `flatten_qa_data` 是过滤的核心([uniseg3d/transforms_3d.py:L560-L629](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L560-L629)),无需真实点云即可单独驱动。

**需要观察的现象**:第一行输出 1 条 QA,第二行输出 0 条且**没有任何异常或告警**;把 `sub_task` 换成 `None` 则输出该层全部。

**预期结果**:如上。推论:若用拼错的 `--sub_tasks` 跑真评估,消耗数小时 GPU 后得到的全是空指标。跑长评估前,先用第 1 步的 grep 校验任务名拼写,是最便宜的保险(待本地验证)。

#### 4.4.5 小练习与答案

**练习 1**:启动器为什么对 part/object 层固定传 `task_type=qa_data`,而不是也置为 `None` 全开?

**答案**:`--sub_tasks` 的语义是「按子任务细分评估」,每个子任务一次独立运行、独立目录([eval_ground3d.sh:L157-L163](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L157-L163))。若 `task_type=None`,同一 `sub_task` 名下的条目会从**所有** task_type 键里收集(L602-L604 的全开逻辑),一旦未来数据在 `qa_data` 之外新增其他键(如历史遗留的 `qa_data_seg`),目录里的结果就不再纯净。显式钉死 `qa_data` 让每次运行的样本集合明确无歧义。

**练习 2**:`--levels` 传了非法值(如 `parta`)和 `--sub_tasks` 传了非法值(如 `distance`),两者的失败表现有何不同?分别在哪一行被拦下(或没被拦下)?

**答案**:`--levels` 的非法值在 Bash 侧就被拦截:[eval_ground3d.sh:L150-L151](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L150-L151) 用 `LEVEL_MAP` 查表,查不到立即报 `bad --levels entry` 退出——因为合法层级只有 3 个,适合白名单。`--sub_tasks` 没有 Bash 侧校验(9 个名字以默认值形式存在于 [L111-L113](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L111-L113),但自定义值不查),拼错会一路传到 [uniseg3d/transforms_3d.py:L613-L616](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L613-L616) 的严格匹配,得到空集、静默零产出。简言之:**层级错=立刻崩,子任务错=安静地空跑**。

**练习 3**:为什么 `multi_turn` 不进入子任务循环,它的样本粒度和 part/object 有什么本质不同?

**答案**:part/object 层的每个元素是**单条问答**(dict),按 9 个任务名归档,可按 `sub_task` 切分;multi_turn 层的每个元素是**一整段对话**(轮次 dict 的列表,[uniseg3d/transforms_3d.py:L575-L586](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L575-L586) 保持其为 list 以便解码器走多轮路径),没有第三级任务键可过滤。评估多轮要考察的是「滚动历史下的连续问答能力」,把轮次拆散反而破坏了考察对象,所以整段一起评。

## 5. 综合实践:画出「checkpoint → 变体 → 配置 → 输出」决策全图

**任务**:假设你同时拿到了两个官方 checkpoint(`Ground3D-LMM-4B-Joint` 与 `Ground3D-LMM-4B-3D` 的 `pytorch_model.pth`),要在 ScanNet 与 ScanNet++ 上完成全量评估。请不实际跑 GPU,完成下面三步并画出决策图。

**步骤 1 —— 干跑核对命令矩阵**:

```bash
DRYRUN=1 bash eval_ground3d.sh --dataset all --levels all \
    --checkpoint /path/to/Joint/pytorch_model.pth
DRYRUN=1 bash eval_ground3d.sh --dataset all --levels all \
    --checkpoint /path/to/Ground3D-LMM-4B-3D/pytorch_model.pth
```

记录两次的 `[variant] rank=... → ...` 探测行,以及各自展开的命令条数与配置文件名。

**步骤 2 —— 探测复验**:用 4.2.4 的 `probe_demo.py` 逻辑(把假 state_dict 换成 `torch.load` 两个真实 checkpoint)独立验证 rank,确认与脚本打印一致。

**步骤 3 —— 画出决策图**:以「checkpoint 文件」为起点,标注每一跳的决策依据与代码行号,应能得到类似下面的图,并把两次干跑的输出树填进叶子节点:

```text
pytorch_model.pth
  │ probe_lora_rank (sh L61-82)
  ├─ rank=16 ──► variant=joint ──► CONFIG_SUFFIX=""  (sh L106)
  │                                 ├─ scannet   → configs/..._scannet.py      → val_outputs_joint/scannet/...
  │                                 └─ scannetpp → configs/..._scannetpp.py    → val_outputs_joint/scannetpp/...
  └─ rank=32 ──► variant=3d    ──► CONFIG_SUFFIX="_3d" (sh L106)
                                    ├─ scannet   → configs/..._scannet_3d.py    → val_outputs_3d/scannet/...
                                    └─ scannetpp → configs/..._scannetpp_3d.py  → val_outputs_3d/scannetpp/...
  每个「数据集 × 变体」叶子下再展开:
    pred_qa_data_val/{part_qa_data/qa_data/<9 个子任务>, object_qa_data/qa_data/<9 个子任务>, multi_turn_qa_data}/
```

**验收标准**:能向别人解释清楚(a)为什么两个 checkpoint 的干跑选择了不同配置;(b)四次「数据集 × 变体」组合的输出目录互不重叠;(c)若把 Joint 的 checkpoint 硬塞给 `_3d` 配置(`--variant 3d` 显式指定),哪一步会发生静默失载、为什么日志里看不到错误(提示:`strict=False` + [configs/ground3dlmm_eval_ground3d_scannet_3d.py:L8-L12](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py#L8-L12) 的 docstring)。步骤 2 对真实权重的探测结果以待本地验证为准。

## 6. 本讲小结

- `eval_ground3d.sh` 是「批量遥控器」:把「数据集 × QA 层级 × 子任务」的笛卡尔积展开成一串 `tools/test.py` 调用,每次调用用 `--cfg-options` 覆盖 `QA_Generation_Test` 的三级旋钮与 `save_pred_qa_dir`,输出目录树本身就是按任务归档的结果。
- 两个发布变体(Joint r=16、3D-only r=32)的 rank 差异会使错配的 LoRA 权重被 `strict=False` **静默丢弃**、模型无声退回基座 Qwen;`--variant auto` 用 `probe_lora_rank` 在加载前扫描 checkpoint 中所有 `lora_A` 权重的 `shape[0]`,据此自动选择配置,rank 混杂或无法识别时宁可报错也不猜。
- 评估配置族是 2×2 矩阵(scannet/scannetpp × joint/_3d):两个 joint 版是从 mmdet3d base 出发的平行完整配置(只差 `data_root` 与 `image_dir`),两个 `_3d` 版各以 27 行继承对应 joint 版、**只覆盖 `lora_r`/`lora_alpha`/`image_dir` 三字段**——变体差异被约束到不可能发散。
- 三级过滤(`task_level`/`task_type`/`sub_task`)全部是**严格匹配且静默**:`--levels` 非法值在 Bash 侧 `LEVEL_MAP` 查表即崩,`--sub_tasks` 拼错则一路传到数据管线得到空集、安静空跑;合法子任务名的权威清单是 `tools/part3d_glamm_data_utils.py` 的 `ALL_TASKS`,须与启动器的 `ALL_SUB_TASKS` 保持同步。
- 工程习惯:跑长评估前先 `DRYRUN=1` 干跑核对命令矩阵与输出目录,再用 `grep` 交叉校验任务名拼写——这两步零成本,能拦下最贵的两类失败。

## 7. 下一步学习建议

- 下一讲 [u6-l3](u6-l3-text-metrics.md) 讲 `tools/evaluate_text_metrics.py` 的 ape / judge / gmdelta 三个阶段——它们消费的本讲输出目录(`val_outputs_*/.../pred_qa_data_val/` 与 `--mask-dump` 的镜像树),建议先掌握本讲的目录结构再读。
- 若想追「掩码落盘」的另一端,回看 [uniseg3d/uniseg3d.py:L1310-L1325](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1310-L1325)(`MASK_DUMP_DIR` 的读取与按场景写 `.npy`),并预告 [u6-l4](u6-l4-grounding-visualization.md) 的 PLY 可视化。
- 若对配置继承想再进一步,可自行阅读 `configs/ground3dlmm_eval_scanrefer*.py` / `ground3dlmm_eval_reason3d*.py` 四个迁移基准配置([u7-l1](u7-l1-scanrefer-reason3d.md) 的主角),检验你能否一眼说出它们与 Ground3D 配置族的差异点。
