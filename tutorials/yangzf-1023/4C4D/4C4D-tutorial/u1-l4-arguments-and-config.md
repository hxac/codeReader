# 第 4 讲：参数体系与 OmegaConf 配置

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `ParamGroup` 如何把「类属性」自动变成命令行参数，以及 `ModelParams` / `PipelineParams` / `OptimizationParams` 三组参数各自管什么。
2. 完整说出一份 `configs/dynerf/*.yaml` 配置从 `OmegaConf.load` 到真正生效的路径，并准确说出 **yaml 与命令行的合并优先级**（提示：与多数工具的约定相反）。
3. 知道 `gaussian_dim`、`time_duration`、`rot_4d`、`force_sh_3d`、`batch_size` 等关键开关控制 4D 高斯的什么行为。
4. 识别三个容易踩的配置坑：改 yaml 里的 `resolution` 不生效、改 yaml 里的 `num_pts` 不生效、改 `iterations` 后最终保存点不跟随。

本讲是单元 1 的收尾：u1-l3 已经画出了 train.py 的整体调用链，本讲把链路最前面的「参数从哪里来」彻底讲透。

## 2. 前置知识

### 2.1 argparse：Python 标准库的命令行解析器

`argparse` 的常规用法是三步：

1. 创建解析器 `parser = ArgumentParser()`；
2. 用 `parser.add_argument("--foo", type=int, default=1)` 声明一个参数；
3. 用 `args = parser.parse_args()` 把 `python train.py --foo 3` 里的 `3` 解析出来。

解析结果装在一个 `Namespace` 对象里，可以用 `args.foo` 或 `vars(args)`（转成字典）访问。`argparse` 还支持把参数分成若干「参数组」（`add_argument_group`），只影响 `--help` 的排版，不影响解析结果。

### 2.2 Python 的 `vars(self)`：实例属性字典

在类的 `__init__` 里写 `self.iterations = 30_000`，就是往实例的 `__dict__` 里塞了一个键值对。`vars(self)` 返回这个字典，且**保持赋值顺序**。4C4D 的参数注册机制正是靠遍历 `vars(self)` 来批量生成命令行参数的——先给属性赋「默认值」，属性的类型顺便充当命令行参数的类型。

### 2.3 为什么需要「命令行 + 配置文件」两套参数

3DGS/4DGS 这类科研代码动辄几十个超参数（学习率、迭代数、致密化阈值……）。全部写命令行太长、容易抄错；全部写死在代码里又没法做实验。4C4D 的折中是：**代码里注册默认值 → 命令行可临时覆盖 → yaml 配置文件批量覆盖**。理解三者的优先级，是改配置不踩坑的前提。

### 2.4 YAML 与 OmegaConf

YAML 是一种「缩进表达层级」的键值文本格式，`rot_4d: True`、`time_duration: [0.0, 10.0]` 都是 YAML 语法。OmegaConf 是一个层次化配置库，`OmegaConf.load(path)` 把 yaml 文件读成 `DictConfig` 对象（可以像字典一样遍历键值），嵌套的 yaml 段会递归成子 `DictConfig`。本讲只需要用到一个特性：遍历 `DictConfig` 的所有叶子键值。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [arguments/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py) | 参数定义包 | `ParamGroup` 注册/提取机制、三个参数子类、遗留的 `get_combined_args` |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | 训练入口 | 脚本级参数注册、`recursive_merge` 合并、合并后的派生覆盖 |
| [configs/dynerf/flame_steak.yaml](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml) | 场景配置 | 四段式结构、与 argparse 默认值的差异 |
| [render.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py) | 推理入口（对照） | 复用同一套参数体系但默认值不同 |
| [scene/dataset_readers.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py) | 数据读取（只看一小段） | `num_pts` 的最终消费方式，用于解释「配置坑 2」 |

configs 目录下共有六份等价结构的场景配置：`coffee_martini.yaml`、`cut_roasted_beef.yaml`、`cook_spinach.yaml`、`flame_salmon.yaml`、`flame_steak.yaml`、`sear_steak.yaml`，全部是 DyNeRF/N3V 数据集的场次。

## 4. 核心概念与源码讲解

### 4.1 模块一：ParamGroup——把类属性变成命令行参数

#### 4.1.1 概念说明

如果老老实实用 argparse，注册 40 多个参数要写 40 多行 `add_argument`，而且默认值散落各处不好查阅。3DGS 家族（包括 4C4D）发明了一个小而美的机制 `ParamGroup`：

- **默认值写在子类的 `__init__` 里**（`self.iterations = 30_000`），一目了然；
- 基类构造函数自动遍历 `vars(self)`，把每个属性注册成同名命令行参数，**属性的类型就是参数类型，属性的值就是默认值**；
- 属性名前加下划线（如 `_source_path`）表示「这个参数额外配一个单字母短选项」；
- `extract()` 做反向操作：从装着所有参数的大 `Namespace` 里，把属于本组的键挑出来，打包成一个小对象传给业务函数。

这样 train.py 只需三行就能注册出三组参数，且「默认值清单」本身就是一份可读的文档。

#### 4.1.2 核心流程

注册（`__init__`）：

```text
子类 __init__ 逐个给 self 赋默认值
        │
        ▼
super().__init__(parser, 组名)
        │
        ▼
parser.add_argument_group(组名)          # 只影响 --help 分组
        │
        ▼
for key, value in vars(self).items():    # 按赋值顺序遍历
    ├─ key 以下划线开头？→ 去掉下划线，额外加 "-首字母" 短选项
    ├─ value 是 bool？   → action="store_true"（只能开关为 True）
    └─ 其他             → type=type(value)
```

提取（`extract`）：

```text
for (k, v) in vars(大 Namespace):
    若 k（或 "_"+k）在本类的属性表里 → 拷贝到 GroupParams 实例
返回这个小实例（例如作为 dataset 传给 training()）
```

一个关键约定：**命令行参数名不带下划线前缀**。`_source_path` 注册出来的是 `--source_path`（外加 `-s`），所以 yaml 里写的键是 `source_path`；`extract` 里同时检查 `k in vars(self)` 和 `"_"+k in vars(self)` 两种拼写，就是为了把 `source_path` 正确匹配回 `_source_path`。

#### 4.1.3 源码精读

**注册机制**（约 20 行，是整个参数体系的心脏）：

[arguments/__init__.py:19-38](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L19-L38) —— `ParamGroup.__init__`。第 21 行创建参数组；第 22 行 `vars(self).items()` 遍历子类刚赋好的属性；第 24-26 行处理下划线短选项（`_source_path` → `--source_path` 加 `-s`，因为 `key[0:1]` 取首字母）；第 27-28 行记下默认值的类型 `t`，再按 `fill_none`（即 `sentinel` 参数）决定是否把默认值换成 `None`；第 29-38 行分四种情况注册：bool 用 `store_true`，其余用 `type=t`。

[arguments/__init__.py:40-45](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L40-L45) —— `extract`。第 43 行的双条件判断兼容「带下划线」与「不带下划线」两种键名拼写，这是 yaml 键能与类属性对上的关键。

**三个参数子类**：

[arguments/__init__.py:47-67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L47-L67) —— `ModelParams`（`--help` 中的 "Loading Parameters" 组）：数据与模型位置相关。要点：`sh_degree = 3` 是球谐阶数上限；五个下划线属性（`_source_path` 数据目录、`_model_path` 输出目录、`_images` 图像子目录、`_resolution` 分辨率缩放、`_white_background`）都带短选项；`eval` 决定是否划分训练/测试相机；`dataloader` 决定图像是否懒加载（u2-l3 展开）；`frame_ratio` 可做抽帧。第 64-67 行的 `extract` 重载把 `source_path` 转成绝对路径。

[arguments/__init__.py:69-78](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L69-L78) —— `PipelineParams`（"Pipeline Parameters" 组）：渲染管线的调试开关与背景贴图。`convert_SHs_python` / `compute_cov3D_python` 是 Python 回退路径（u4-l3 展开）；`eval_shfs_4d` 为 `True` 时时间球谐阶数取 2（见 [train.py:63](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L63) 的 `sh_degree_t=2 if pipe.eval_shfs_4d else 0`）；`env_map_res` 大于 0 时启用可优化球面背景。

[arguments/__init__.py:80-109](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L80-L109) —— `OptimizationParams`（"Optimization Parameters" 组）：全部训练超参。`iterations = 30_000` 总迭代数；`position_lr_*` 是位置学习率调度；`coefficient_lr` / `coefficient_weight_decay` 服务于衰减网络 Coefficient（u6-l1 展开）；`densify_*` 与 `opacity_reset_interval` 驱动致密化/剪枝调度（u5-l4 展开）；`lambda_dssim` 是 SSIM 损失权重（u5-l2 展开）。注意第 102 行的时间梯度阈值默认写成了 `0.0002 / 40`——这是 Python 表达式，不是字符串，运行时就是 \(5\times10^{-6}\)。

**一段遗留代码**（承接 u1-l3「识别遗留代码」的主题）：

[arguments/__init__.py:111-131](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L111-L131) —— `get_combined_args`。这是 3DGS 原版 render.py 用来读取训练时写下的 `cfg_args` 文件并与命令行合并的函数。用 `grep` 检索全仓库可以发现它**只有定义、没有任何调用**——4C4D 的 render.py 已改用与 train.py 相同的 `--config` + OmegaConf 方案（[render.py:165-174](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L165-L174)）。它读的 `cfg_args` 文件倒是仍在生成（见 4.2.3）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「类属性 → 命令行参数」的注册与提取机制。

**操作步骤**：

1. 在仓库根目录新建临时文件 `tmp_demo_paramgroup.py`（这是示例代码，读者自建，勿放进仓库）：

```python
# 示例代码：ParamGroup 机制演示
import sys
sys.path.insert(0, ".")            # 让 Python 找到仓库根目录的 arguments 包
from argparse import ArgumentParser
from arguments import ParamGroup    # arguments 包只 import argparse/sys/os，无需 GPU

class MyParams(ParamGroup):
    def __init__(self, parser):
        self.learning_rate = 0.01   # → --learning_rate，float，默认 0.01
        self._output_dir = "out"    # → --output_dir，另加短选项 -o
        self.verbose = False        # → --verbose，store_true
        super().__init__(parser, "My Group")

parser = ArgumentParser(description="ParamGroup demo")
MyParams(parser)
parser.print_help()

args = parser.parse_args(["--learning_rate", "0.1", "-o", "demo", "--verbose"])
print(vars(args))   # {'learning_rate': 0.1, 'output_dir': 'demo', 'verbose': True}
```

2. 运行 `python tmp_demo_paramgroup.py`（只依赖标准库，不需要 PyTorch/GPU）。

**需要观察的现象**：

- `--help` 输出中出现 "My Group" 分组，含 `--learning_rate`、`--output_dir`、`-o`、`--verbose`；
- 命令行解析后 `verbose` 变成 `True`——注意 `store_true` 意味着**不存在把它传成 False 的写法**；
- 短选项 `-o` 与 `--output_dir` 等价。

**预期结果**：打印的 `vars(args)` 与上面注释一致；`--help` 中三个参数按 `__init__` 里的赋值顺序排列。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`_source_path` 的下划线前缀带来了什么额外效果？
**答案**：除了注册 `--source_path`，还会注册单字母短选项 `-s`（取属性名首字母，见 [arguments/__init__.py:24-26](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L24-L26) 及第 31-33 行），方便命令行简写。同理 `-m` 对应 `model_path`、`-r` 对应 `resolution`。

**练习 2**：`iterations` 的参数类型（`type=int`）是怎么确定的？如果把它默认值改成 `30_000.0` 会怎样？
**答案**：类型取自默认值的类型：`t = type(value)`（[arguments/__init__.py:27](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L27)）。若默认值写成浮点，参数就变成 `type=float`，后续 `range(first_iter, opt.iterations)` 之类按整数使用它的代码会出错——这也是为什么默认值必须谨慎选择字面量类型。

**练习 3**：`ModelParams` 里的 `eval` 是 bool（`store_true`，默认 False）。用户能通过命令行显式传 `--eval False` 关掉吗？
**答案**：不能。`store_true` 只会把标志位置成 True，`--eval False` 里的 "False" 会被当成无关位置参数导致报错（或被忽略）。要改 bool 只能改 yaml（yaml 合并对 bool 是直接赋值，可写 `eval: False`，见 4.2）。

### 4.2 模块二：train.py 的参数注册与 OmegaConf 合并

#### 4.2.1 概念说明

train.py 的参数分两层：

1. **三组共享参数**：`ModelParams` / `OptimizationParams` / `PipelineParams`，由 train.py 与 render.py 共用（保证训练与推理读同一套配置语义）；
2. **train.py 独有的脚本级参数**：直接 `parser.add_argument` 注册，包括本讲学习目标里的关键开关（`gaussian_dim`、`time_duration`、`rot_4d` 等）以及论文核心的 opacity decay 参数。

参数的生命周期是：**argparse 解析 → OmegaConf 递归合并 yaml → 一串「事后派生覆盖」→ 固化到输出目录 → `extract` 拆分后传入 `training()`**。其中「合并方向」和「事后覆盖」两处最容易误判，下面逐一拆开。

#### 4.2.2 核心流程

一次 `python train.py --config configs/dynerf/flame_steak.yaml ...` 的参数流水线：

```text
① argparse 解析命令行                        train.py L431
       │  （此时所有值都是代码默认值或命令行传入值）
② save_iterations 追加一次 iterations         train.py L432   ← 注意：用的是合并前的默认值！
③ OmegaConf.load(args.config)                train.py L434   ← --config 事实上必填
④ recursive_merge：把 yaml 叶子键无条件       train.py L435-L443
   setattr 进 args  →  yaml 覆盖命令行（与常见约定相反！）
⑤ 派生覆盖（在合并之后执行，优先级最高）        train.py L445-L474
   ├─ exhaust_test → 补充 test_iterations
   ├─ initial_num_pts → num_pts（守卫条件恒真）
   ├─ max_num_pts → densify_until_num_points
   ├─ res → resolution（守卫条件恒真）
   ├─ weight_decay → coefficient_weight_decay
   └─ opacity_decay=True → densify_until_iter = iterations
⑥ 输出目录检查/创建、training_params.txt 固化  train.py L463-L479
⑦ lp/op/pp.extract(args) 拆成三小组           train.py L489-L492
   传入 training(dataset, opt, pipe, ...)
```

把第 ①④⑤ 步的优先级写成公式（\(k\) 为某个参数键名）：

\[
\text{final}(k)=
\begin{cases}
\text{post}(k), & k\in\{\,\text{res}\!\to\!\text{resolution},\ \text{initial\_num\_pts}\!\to\!\text{num\_pts},\ \text{weight\_decay}\!\to\!\text{coefficient\_weight\_decay}},\dots\}\\[4pt]
\text{yaml}(k), & k\in \text{keys(yaml)}\\[4pt]
\text{cmdline}(k), & \text{其他}
\end{cases}
\]

**yaml 覆盖命令行**，而**第 ⑤ 步的派生覆盖又压过 yaml**——这与「命令行优先」的常见工具约定正好相反。

#### 4.2.3 源码精读

**三组共享参数注册**：

[train.py:376-381](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L376-L381) —— 创建解析器并实例化三个参数组。实例化动作本身就把几十个参数注册进了 `parser`。

**train.py 独有的脚本级参数**（分五段读）：

- [train.py:382-388](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L382-L388)：`--config`（无默认值，配合第 434 行的 `OmegaConf.load`，**不传即报错**，所以事实上必填）、`--debug_from`、`--detect_anomaly`、`--test_iterations` / `--save_iterations`（`nargs="+"`，可接多个整数）、`--quiet`、`--start_checkpoint`。
- [train.py:390-400](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L390-L400)：**4D 表示的关键开关**（详见下表）。
- [train.py:402-409](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L402-L409)：输出与视角划分。`--training_view` 默认 `"1,10,13,20"`（4 台训练相机编号），`--testing_view` 默认空。
- [train.py:411-419](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L411-L419)：**opacity decay（Neural Decaying Function）参数组**。注意 `--opacity_decay` 是 `default=True` 的 `store_true`——即 4C4D **默认开启衰减**；`f_min`/`f_max`（默认 0.996/0.998）限定衰减因子范围，`decay_from_iter`（默认 500）是启用时机（u6-l3 展开）。
- [train.py:421-429](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L421-L429)：杂项。`--res`（默认 1）、`--redundant_ratio`、`--downsample_method`（`fps`/`random`）、`--test_per_iter`（默认 1500）、`--time_aware`（默认 True）、`--reset_opacity` / `--add_size_threshold`（默认 False）。

关键开关速查表（学习目标要求掌握的三个加粗）：

| 参数 | 默认（train.py） | 含义 |
|---|---|---|
| **`gaussian_dim`** | 4 | 高斯维度：4 表示带时间维的 4D 高斯；3 退回 3D 高斯。决定 `GaussianModel` 是否创建 `_t`、`_scaling_t` 等时间属性（u3-l2） |
| **`time_duration`** | `[0, 10.0]` | 时间参数化区间。所有帧的 timestamp 被归一化到该区间（u2-l2 讲归一化公式）；`frame_ratio > 1` 时会按比例压缩（[train.py:51-52](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L51-L52)） |
| **`rot_4d`** | False（store_true） | 是否使用完整 4D 旋转（xyzt 四维协方差）。True 时分裂新高斯也在 xyzt 空间采样（u3-l3、u5-l4）。flame_steak.yaml 里设为 True |
| `force_sh_3d` | False（store_true） | 强制只用 3D 球谐、不启用时间方向球谐 |
| `batch_size` | 1 | 每次迭代同时渲染几张相机图像，4DGS 式多视角 batch 训练（u5-l3）。yaml 里设为 4 |
| `exhaust_test` | False（store_true） | 开启后每 `test_per_iter` 次迭代补一个测试点，得到平滑的测试 PSNR 曲线 |
| `opacity_decay` | **True** | 是否启用 Neural Decaying Function（u6 全单元） |
| `training_view` | `"1,10,13,20"` | 参与训练的相机编号，逗号分隔 |

**OmegaConf 递归合并（本模块核心）**：

[train.py:431-432](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L431-L432) —— 第 431 行先解析命令行；第 432 行把「当前的」`args.iterations`（此刻还是 argparse 默认值 30000，**yaml 还没合并**）追加进 `save_iterations`。这是坑 3 的根源。

[train.py:434-443](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L434-L443) —— 加载 yaml 并递归合并：`recursive_merge` 遇到 `DictConfig`（嵌套段）就继续下钻，遇到叶子就用 `setattr(args, key, host[key])` **无条件覆盖**。两个推论：

1. **合并方向是「yaml 压命令行」**。命令行里若传了 yaml 也有的键（如 `--iterations 20000` 对 yaml 的 `iterations: 30_000`），最终生效的是 yaml 值。README 推荐命令里的 `--training_view`、`--output_dir` 之所以有效，是因为 yaml 里**没有**这两个键。
2. 第 440 行 `assert hasattr(args, key), key` 构成**白名单**：yaml 只能写代码里已注册的键，写新键或拼错键名会直接断言失败（比静默忽略友好）。同时，yaml 的嵌套段名（`ModelParams:` 等）只是排版——合并后所有叶子键都平铺进同一个 `args`，真正的分组还原靠第 489-492 行的三个 `extract`。

**合并后的派生覆盖（三个坑）**：

- **坑 1（合并方向）**：如上，想让命令行赢，只能保证该键不出现在 yaml 里。另一个连带限制：`rot_4d` 是 `store_true`，yaml 写 `rot_4d: True` 后，**不存在任何命令行写法能把它关掉**（没有 `--no-rot_4d`）。想做「关闭 4D 旋转」的消融必须改 yaml。

- **坑 2（恒真的守卫条件）**：[train.py:448-455](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L448-L455) 的两个 `if ... is not None` 判断，守卫的对象默认值分别是 `-1`（`--initial_num_pts`，[L392](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L392)）和 `1`（`--res`，[L422](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L422)）——整数永远「is not None」，条件恒真。于是：
  - `args.resolution = args.res` 无条件执行 → yaml 里 `ModelParams.resolution: 2` **总会被默认值 1 覆盖**。想改渲染分辨率只能 `--res 2`，或在 yaml **顶层**加 `res: 2`（顶层键在合并时覆盖 `args.res`，再由它覆盖 `resolution`）。[train.py:460-461](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L460-L461) 的 `weight_decay → coefficient_weight_decay` 是同款逻辑（默认 1e-4 非空，恒覆盖）。
  - `args.num_pts = args.initial_num_pts` 同样恒真 → yaml 顶层的 `num_pts: 300_000` **总会被默认值 -1 覆盖**。而 `num_pts = -1` 传到数据读取端后，[scene/dataset_readers.py:324](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L324) 的下采样条件要求 \( \text{num\_pts} > 0 \) 不成立 → **不做下采样，直接使用 sparse/0 里的完整初始点云**。想控制初始点数应使用 `--initial_num_pts` 或 yaml 顶层 `initial_num_pts:`。（`--max_num_pts` 的默认是 `None`，那个判断写得是对的，只有显式传参才覆盖。）

- **坑 3（时机错误的 append）**：[train.py:432](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L432) 在合并**前**执行 `args.save_iterations.append(args.iterations)`，追加的是默认 30000。若把 yaml 改成 `iterations: 20_000`，训练循环会在 20000 结束，而 `save_iterations` 是 `[7000, 30000, 30000]`——**最后一次保存落在 7000，收尾 checkpoint 不会生成**。解决：命令行补 `--save_iterations 7000 20000 --test_iterations 7000 20000`（这两个键不在 yaml 里，命令行值能存活）。另注意 [train.py:445-446](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L445-L446) 的 `exhaust_test` 补测试点用的是 `op.iterations`——`op` 是 `OptimizationParams` **实例**，它身上的 `iterations` 是 [arguments/__init__.py:82](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L82) 的类默认 30000，不会随 yaml 变（`extract` 返回的是新对象，不回写 `op`）。

**联动与固化**：

- [train.py:473-474](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L473-L474)：`opacity_decay` 为真时 `densify_until_iter = iterations`——衰减开启后致密化贯穿全程（这正是 u1-l1 讲过的「开启衰减 → 致密化不提前停止」联动）。这一步发生在合并之后，用的是合并后的 `iterations`。
- [train.py:463-465](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L463-L465)：输出目录已存在则直接抛错——这也是 README 每次训练都要带 `--output_dir` 的原因之一。
- [train.py:476-479](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L476-L479)：把最终 `args` 全量写进 `training_params.txt`；[train.py:294-295](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L294-L295) 另写一份 `cfg_args`（3DGS 遗留格式，供已无人调用的 `get_combined_args` 风格工具读取）。**排错时先看这两个文件**，它们记录的才是真正生效的配置。
- [train.py:489-492](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L489-L492)：三个 `extract` 把平铺的 `args` 拆成 `dataset`（lp）、`opt`（op）、`pipe`（pp）三小组传入 `training()`；脚本级参数（`gaussian_dim`、`batch_size` 等）则单独按名传入。

**render.py 对照**（同一套机制，默认值不同）：

[render.py:165-177](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L165-L177) —— 与 train.py 逐字几乎相同的 `recursive_merge` 与 `res` 覆盖。但脚本级默认不同，如 [render.py:130-131](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L130-L131) 的 `gaussian_dim` 默认 3、`time_duration` 默认 `[-0.5, 0.5]`（train.py 是 4 与 `[0, 10.0]`）。两入口的行为一致性完全靠「同一份 yaml 把默认值都盖掉」来保证——所以训练用什么 config，渲染就必须用什么 config。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：复制官方配置为自定义版本，改分辨率与迭代数，用 `--help` 与源码核对「每个键从哪来、最终谁说了算」。

**操作步骤**：

1. 复制配置：`cp configs/dynerf/flame_steak.yaml configs/dynerf/my_flame_steak.yaml`。
2. 编辑副本：`iterations: 20_000`；`ModelParams.resolution: 4`；`model_path` 改成一个新目录。
3. 查看帮助：`python train.py --help`（需先 `conda activate 4dgs`）。**注意**：train.py 顶层 import 了 CUDA 扩展（u1-l2 讲过），`--help` 也要等所有 import 成功才轮到 argparse，环境不全时会先报 `ModuleNotFoundError`。此时可用无需 GPU 的替代方案——`arguments` 包只依赖标准库：

```bash
python -c "
from argparse import ArgumentParser
from arguments import ModelParams, PipelineParams, OptimizationParams
p = ArgumentParser()
ModelParams(p); OptimizationParams(p); PipelineParams(p)
p.print_help()"
```

4. 对照 `--help` 与 my_flame_steak.yaml，把 yaml 里的每个键填进下表（答案见「预期结果」）。

**需要观察的现象**：

- `--help` 里出现 "Loading Parameters"、"Optimization Parameters"、"Pipeline Parameters" 三个分组，以及一大块不属于任何分组的脚本级参数；
- 三组的参数顺序与 `arguments/__init__.py` 中 `__init__` 的赋值顺序一致。

**预期结果**（键的分类答案）：

| yaml 位置 | 键 | 注册来源 |
|---|---|---|
| 顶层 L1-L8 | `gaussian_dim`、`time_duration`、`num_pts`、`num_pts_ratio`、`rot_4d`、`force_sh_3d`、`batch_size`、`exhaust_test` | train.py 脚本级参数（[L390-L400](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L390-L400)） |
| `ModelParams:` 段 | 13 个键（`sh_degree`…`dataloader`） | `ModelParams`（arguments L47-L62） |
| `PipelineParams:` 段 | 7 个键（`convert_SHs_python`…`eval_shfs_4d`） | `PipelineParams`（arguments L69-L77） |
| `OptimizationParams:` 段 | 27 个键（`iterations`…`lambda_motion`） | `OptimizationParams`（arguments L81-L108） |

再预测两个改动的实际效果（这一步是本实践的点睛）：

- `ModelParams.resolution: 4` → **不生效**，最终 resolution = 1（被 `--res` 默认值覆盖，坑 2）。要让改动生效需 `--res 4` 或顶层加 `res: 4`。
- `iterations: 20_000` → 生效（`opt.iterations` 就是 20000），但**收尾保存点不会自动跟着变**（坑 3），需命令行补 `--save_iterations 7000 20000 --test_iterations 7000 20000`。

以上均为代码阅读结论；实际训练行为待本地验证（需要 GPU 与数据集）。

#### 4.2.5 小练习与答案

**练习 1**：命令行 `python train.py --config configs/dynerf/flame_steak.yaml --iterations 20000`，最终 `opt.iterations` 是多少？
**答案**：30000。`--iterations` 确实把 `args.iterations` 解析成 20000，但第 434-443 行的合并会把 yaml 里的 `iterations: 30_000` 无条件 `setattr` 回去（合并方向是 yaml 压命令行）。`iterations` 键在 yaml 里，所以命令行输；要改只能在 yaml 里改。

**练习 2**：为什么 README 的训练命令只传 `--config`、`--training_view`、`--output_dir` 三个参数就够了？
**答案**：`--config` 是必填（`OmegaConf.load(None)` 会报错）；另外两个是 yaml 中不存在的键，命令行值能活过合并；其余所有超参都由 yaml 一次性给足。反过来，任何「yaml 已有」的键想临时改值，改命令行无效，必须改 yaml。

**练习 3**：`op.iterations`（`op` 是 `OptimizationParams` 实例）与 `opt.iterations`（`opt = op.extract(args)`）有何区别？
**答案**：前者永远是类默认 30000——`extract` 从 `args` 拷贝出一个新的 `GroupParams`，不会回写 `op` 自身；后者是合并后的值。[train.py:445-446](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L445-L446) 用的是前者（坑 3 的另一半），而训练循环用的是后者，两者在 yaml 改过 `iterations` 时会不一致。

### 4.3 模块三：configs/dynerf/*.yaml——一份配置的解剖

#### 4.3.1 概念说明

`configs/dynerf/` 下六份配置对应 DyNeRF/N3V 数据集的六个场次，结构完全一致、只有路径与数值不同。以 `flame_steak.yaml` 为例，它分四段：

1. **顶层键**：对应 train.py 脚本级参数（4D 表示开关与批量/测试策略）；
2. **`ModelParams:` 段**：数据与输出位置；
3. **`PipelineParams:` 段**：渲染管线开关；
4. **`OptimizationParams:` 段**：训练超参。

段名只是给人看的组织结构——`recursive_merge` 递归到叶子就平铺，**段名甚至不必与参数组同名**。真正硬性的约束是：**叶子键名必须与某个已注册的 argparse 属性同名**（`assert hasattr`，[train.py:440](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L440)）；键名不带下划线前缀（写 `source_path` 而非 `_source_path`，与命令行 `--source_path` 的 dest 一致）。yaml 里没写的键保持 argparse 默认值——flame_steak.yaml 把三个组的键写全了，但理解「没写 = 用默认」对读其他配置很重要。

#### 4.3.2 核心流程

一份 yaml 的生效路径（承接 4.2.2 的流水线，从 yaml 视角看）：

```text
flame_steak.yaml
  ├─ 顶层 8 个键 ────┐
  ├─ ModelParams 段 ─┤ recursive_merge 平铺覆盖 args
  ├─ PipelineParams ─┤ （同名键：yaml 值生效）
  └─ OptimizationParams 段
                │
                ▼
        随后仍可能被派生覆盖（res、initial_num_pts、weight_decay、opacity_decay 联动）
                │
                ▼
        固化进 training_params.txt / cfg_args
```

与 argparse 默认值相比，flame_steak.yaml 改动的关键值：

| 键 | argparse 默认 | yaml 值 | 备注 |
|---|---|---|---|
| `rot_4d` | False | True | 启用完整 4D 旋转 |
| `batch_size` | 1 | 4 | 4 台相机一起训（与 4C4D 的 4 相机设定呼应） |
| `dataloader` | False | True | 图像懒加载（u2-l3） |
| `eval` / `eval_shfs_4d` | False | True | 划分测试相机 / 启用 4D 球谐 |
| `opacity_reset_interval` | 3000 | 10000 | 不透明度重置周期放慢 |
| `densify_until_num_points` | -1 | 4200000 | 致密化点数上限（-1 表示不限） |
| `resolution` | -1 | 2 | **会被 `--res` 默认值 1 覆盖（坑 2）** |
| `num_pts` | 100000 | 300_000 | **会被 `initial_num_pts` 默认值 -1 覆盖（坑 2）** |

另外注意 yaml 的数值写法：`30_000`（数字下划线）、`0.0002 / 40`（算术表达式）、`1e-5`（科学计数）、`[0.0, 10.0]`（列表）。OmegaConf 的解析器支持数值中的下划线分隔与叶子上的算术运算，因此这些写法读进来就是数值 \(5\times 10^{-6}\)、30000，而不是字符串——这属于 OmegaConf 的语法特性，待本地验证（见 4.3.4）。

#### 4.3.3 源码精读

[configs/dynerf/flame_steak.yaml:1-8](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L1-L8) —— 顶层：4D 表示与训练策略。`gaussian_dim: 4` + `rot_4d: True` + `time_duration: [0.0, 10.0]` 共同声明「训练 xyzt 四维旋转的 4D 高斯，时间归一化到 [0,10)」；`num_pts`/`num_pts_ratio` 与初始点云规模相关（注意坑 2）；`batch_size: 4` 四相机同批；`exhaust_test: True` 每 1500 迭代测一次。

[configs/dynerf/flame_steak.yaml:10-23](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L10-L23) —— `ModelParams` 段：`source_path` 指向 COLMAP 格式数据目录（含 `images/` 与 `sparse/0/`，u2-l1 展开），`model_path` 是输出目录，`resolution: 2`（会被覆盖，见坑 2），`dataloader: True`、`eval: True`。

[configs/dynerf/flame_steak.yaml:25-32](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L25-L32) —— `PipelineParams` 段：两个 Python 回退开关均为 False（用 CUDA 实现），`eval_shfs_4d: True` 使 [train.py:63](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L63) 传入 `sh_degree_t=2`，即时间方向也用 2 阶球谐（u3-l4 展开）。

[configs/dynerf/flame_steak.yaml:34-61](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L34-L61) —— `OptimizationParams` 段：`iterations: 30_000`、位置学习率调度（`position_lr_init/final/delay_mult/max_steps`）、各属性学习率、`coefficient_lr`/`coefficient_weight_decay`（衰减网络）、`lambda_dssim: 0.2`（SSIM 权重）、`densify_grad_t_threshold: 0.0002 / 40`（时间梯度阈值，解析为 \(5\times10^{-6}\)）、`densify_until_num_points: 4200000`。对比 argparse 默认可见该场景把 `opacity_reset_interval` 从 3000 放宽到 10000。

#### 4.3.4 代码实践

**实践目标**：确认 yaml 数值语法的实际解析结果，并亲手让 `resolution` 真正生效。

**操作步骤**：

1. 用 OmegaConf 验证数值写法（任意装有 omegaconf 的环境即可，无需 GPU）：

```python
# 示例代码：验证 OmegaConf 对 4C4D 配置中数值写法的解析
from omegaconf import OmegaConf

cfg = OmegaConf.load("configs/dynerf/flame_steak.yaml")
print(type(cfg.num_pts), cfg.num_pts)                                  # 期待 int 300000
print(type(cfg.OptimizationParams.densify_grad_t_threshold),
      cfg.OptimizationParams.densify_grad_t_threshold)                 # 期待 float 5e-06
print(type(cfg.time_duration), cfg.time_duration)                      # 期待列表 [0.0, 10.0]
```

2. 在自定义副本的**顶层**（不要放进 `ModelParams:` 段）加一行 `res: 4`，再按 4.2.4 的推导预测最终 `resolution`：合并阶段顶层 `res` 覆盖 `args.res=4`，随后 `args.resolution = args.res` 得 4——这次分辨率真的变了。
3. 对比另外五份配置：`diff configs/dynerf/flame_steak.yaml configs/dynerf/sear_steak.yaml`，观察差异是否只集中在路径与少数超参。

**需要观察的现象**：

- 步骤 1 打印的类型均为数值/列表而非字符串；
- 步骤 3 的 diff 只有少量行（路径、个别阈值）。

**预期结果**：`num_pts` 为 300000（int）、时间梯度阈值为 \(5\times 10^{-6}\)（float）、`time_duration` 为 `[0.0, 10.0]`。若你的 OmegaConf 版本较老把 `0.0002 / 40` 解析成字符串，训练时会在类型检查处暴露——这正是值得「待本地验证」的一点。

#### 4.3.5 小练习与答案

**练习 1**：在 yaml 顶层写 `foo: 1` 会发生什么？
**答案**：启动即失败。`recursive_merge` 在 [train.py:440](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L440) 断言 `hasattr(args, key)`，`foo` 不是任何已注册参数，`assert` 报错并打印键名 `foo`。这个白名单能帮你尽早发现拼错的键名。

**练习 2**：flame_steak.yaml 的 `densify_grad_t_threshold: 0.0002 / 40` 最终数值是多少？为什么要这样写？
**答案**：\(0.0002/40 = 5\times10^{-6}\)。这样写把「时间梯度阈值 = 空间梯度阈值 `densify_grad_threshold` 的 1/40」这层比例关系直接写在配置里，改一处比例一目了然（前提是 OmegaConf 求值了该表达式）。

**练习 3**：`exhaust_test: True` 时，测试点是怎么算出来的？如果 yaml 改了 `iterations`，测试点会覆盖到最后一次迭代吗？
**答案**：[train.py:445-446](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L445-L446) 在默认 `[7000, 30000]` 基础上追加 `range(0, op.iterations, test_per_iter)`。由于用的是 `op.iterations`（恒为 30000 的类默认）而非合并后的 `args.iterations`，改 yaml 的 `iterations` 不会让这组测试点跟随——例如改成 20000 时测试点最多到 19500（1500 的倍数），最后一轮迭代仍不会被测试。

## 5. 综合实践

**任务：为自己的第一次训练做一份「参数流向审计表」。**

1. **准备**：`cp configs/dynerf/flame_steak.yaml configs/dynerf/my_first_run.yaml`；把 `source_path` 指向你的数据目录（结构见 README 的 Dataset Structure，u2 单元会详解），`model_path` 改为新目录；`iterations` 改为 `6_000`（先短跑验证）。
2. **修正两个坑**：在 yaml 顶层加 `res: 2`（真正控制分辨率）；命令行准备 `--save_iterations 6000 --test_iterations 6000`（让短跑也能落盘收尾 checkpoint）。
3. **产出审计表**：任选 10 个键（建议含 `gaussian_dim`、`time_duration`、`rot_4d`、`resolution`、`num_pts`、`iterations`、`densify_until_iter`、`lambda_dssim`、`f_min`、`training_view`），逐列填写：

| 键 | 在哪注册（文件:行） | argparse 默认 | yaml 是否覆盖 | 合并后是否再被派生覆盖 | 最终值 | 被谁消费（下游函数/参数） |
|---|---|---|---|---|---|---|

   提示：最后一列可对照 [train.py:48-67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L48-L67) 里 `training()` 如何把这些值分发给 `GaussianModel`、`Scene`、`render`。
4. **验证**：有 GPU 与数据时，运行 `python train.py --config configs/dynerf/my_first_run.yaml --training_view 1,10,13,20 --output_dir exp1 --save_iterations 6000 --test_iterations 6000`，打开输出目录里的 `training_params.txt` 与 `cfg_args`，逐键核对是否与你的预测一致。无 GPU 时，审计表前六列纯靠读代码即可完成，最后一列的运行核对标注「待本地验证」。

## 6. 本讲小结

- `ParamGroup` 用「默认值即类属性」的方式批量注册命令行参数：属性类型即参数类型、属性值即默认值，下划线前缀额外换来单字母短选项；`extract` 再按属性名把参数从大 `Namespace` 里拆回三小组。
- 参数分两层：三组共享参数（`ModelParams`/`PipelineParams`/`OptimizationParams`，train.py 与 render.py 复用）+ 入口各自独有的脚本级参数；`gaussian_dim`、`time_duration`、`rot_4d` 等关键开关属于后者。
- 合并优先级是「派生覆盖 > yaml > 命令行 > 默认值」，与常见「命令行最高」的约定相反；`recursive_merge` 无条件 `setattr`，且 `assert hasattr` 构成键名白名单。
- 三个坑都源于执行顺序：`--res`/`--initial_num_pts` 的 `is not None` 守卫恒真导致 yaml 的 `resolution`、`num_pts` 总被覆盖；`save_iterations.append` 发生在合并前导致改 `iterations` 后收尾保存点不跟随。
- 排错时先读输出目录里的 `training_params.txt` 与 `cfg_args`——它们记录的才是真正生效的配置快照。
- `arguments/get_combined_args` 是 3DGS 遗留代码，全仓库无调用；识别这类「死代码」能避免在错误的地方找逻辑。

## 7. 下一步学习建议

本讲结束即完成单元 1（环境、目录、入口、参数四大基础设施）。下一步进入单元 2「数据加载与场景构建」：

- **u2-l1（COLMAP 格式）**：`source_path` 指向的 `sparse/0/` 三个二进制文件到底装了什么、`colmap_loader.py` 怎么读。
- **u2-l2（readColmapSceneInfo）**：本讲埋下的 `num_pts`（坑 2）与 `time_duration`、`training_view` 将在数据读取端被真正消费，值得回头印证。
- 若你更关心训练侧，可先跳到 **u5-l1（训练主循环）**，看 `iterations`、`densify_*`、`save_iterations` 如何驱动整个循环；**u6 系列**再回来解释 `opacity_decay` 组参数与 `densify_until_iter` 联动的完整含义。
