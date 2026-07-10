# run.py：程序化训练、评测与渲染

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 `scripts/run.py` 如何用一个 Python 脚本复刻并扩展 `instant-ngp` 命令行的全部能力（加载、训练、快照、评测、截图、视频、网格）。
- 读懂 `--test_transforms` 这条评测链：加载测试集 → 逐图渲染 → 计算 MSE/PSNR/SSIM。
- 看懂 `--screenshot_transforms` 如何按相机位姿批量出图，以及 `--video_camera_path` 如何逐帧渲染并用 ffmpeg 合成 mp4。
- 认识 `Rfl`/`RflRelax` 训练模式的「时间段调度」逻辑：在训练的不同阶段自动切换 `TrainMode`。

本讲是上一篇 [u7-l1 pyngp 绑定架构](u7-l1-pyngp-bindings.md) 的承接——上一篇讲清了「pyngp 这个模块是怎么把 C++ `Testbed` 暴露给 Python 的」，本讲讲清「有人把这套 API 串成了一个可复用的自动化脚本 `run.py`」。

## 2. 前置知识

本讲默认你已经理解以下概念（均来自前置讲义，这里只做最短的回忆）：

- **Testbed「上帝对象」与帧循环**（u2-l1、u2-l2）：`pyngp.Testbed` 承载全部状态，`testbed.frame()` 每调用一次就推进一帧（训练一步并按需渲染），无窗口时仍可工作。
- **文件加载分发**（u2-l3、u4-l1）：`load_file` / `load_training_data` 会根据文件类型自动判别模式；NeRF 数据用 `transforms.json` 描述相机内外参。
- **pyngp 关键 API**（u7-l1）：`testbed.render(width, height, spp, linear, ...)` 返回 numpy 图像数组；`testbed.nerf.training.train_mode` 是一个可读写的 `TrainMode` 枚举。

如果你对上面任意一点陌生，建议先回到对应讲义。本讲只做「把这些零件组装成一个脚本」的讲解，不再重复零件本身的原理。

几个本讲会用到的 Python 库术语：

- **argparse**：Python 标准库的命令行参数解析器，用 `add_argument` 注册参数，`parse_args()` 得到一个带属性的对象。
- **tqdm**：一个进度条库，`tqdm(range(N))` 会在终端打印一个会走动的进度条。
- **commentjson**：兼容 JSON 但允许注释的解析库，本仓库的 `transforms.json` 用它读取。
- **PSNR / SSIM**：图像质量指标，本讲第 4.3 节会给出定义。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [scripts/run.py](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py) | **本讲的主角**。约 400 行，把「构造 Testbed → 加载 → 训练 → 评测/出图/出视频/出网格」串成一条命令行流水线。 |
| [scripts/common.py](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/common.py) | 通用工具：图像读写（`write_image`/`read_image`）、sRGB↔线性转换、误差指标（`compute_error`/`SSIM`）、`mse2psnr`。`run.py` 用 `from common import *` 全量引入。 |
| [scripts/scenes.py](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/scenes.py) | 「场景名 → 数据目录」的映射表，以及 SDF 场景的相机/材质预设（`setup_colored_sdf`）、快照默认文件名（`default_snapshot_filename`）。 |
| [scripts/constants.py](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/constants.py) | 路径常量：`ROOT_DIR`、`NGP_DATA_FOLDER`、`NERF_DATA_FOLDER` 等，决定去哪里找数据。 |
| [src/python_api.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu) | pyngp 绑定源头（u7-l1 已详述）。本讲只引用其中 `render`、`frame`、`TrainMode` 几处签名，说明 `run.py` 调用的方法从何而来。 |

一句话定位：`run.py` 是「胶水」，把 pyngp 暴露的能力按典型科研/出片工作流编排好，让你用一条命令完成「训练 → 评测 → 出图 → 出视频」。

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**run.py 主流程**、**训练循环与 Rfl/RflRelax 调度**、**--test_transforms 评测**、**批量截图与视频渲染**。

### 4.1 run.py 主流程：参数解析与 scene 名映射

#### 4.1.1 概念说明

上一篇 u7-l1 我们看到，pyngp 把 `Testbed` 整个类暴露给了 Python，于是你可以在 Python 里手写「构造 → load → 训练循环 → render」。但每次都手写这段样板代码很烦，而且每个科研任务（评测 PSNR、批量截图、出视频）都需要不同的参数组合。

`run.py` 解决的就是这个问题：它用 `argparse` 把几十个命令行开关映射成对 `Testbed` 的调用，把「加载什么、训练多少步、要不要评测/截图/视频/网格」全部参数化。你可以把它理解成「pyngp 的官方示例 + 评测脚手架」。

#### 4.1.2 核心流程

`run.py` 的 `if __name__ == "__main__"` 主流程是一条严格的顺序流水线，每一步都对应一个 pyngp 调用：

```
parse_args()                       # 1. 解析命令行
  ↓
ngp.Testbed() + root_dir           # 2. 构造 Testbed
  ↓
load_file / load_training_data     # 3. 加载数据（位置参数或 --scene）
  ↓
init_window / init_vr（仅 GUI）     # 4. 可选：开窗口
  ↓
load_snapshot 或 reload_network    # 5. 可选：加载快照或网络配置
  ↓
设置训练相关开关                    # 6. train_mode / nerf_compatibility / sharpen ...
  ↓
while testbed.frame(): 训练循环     # 7. 训练到 n_steps
  ↓
save_snapshot / test_transforms /  # 8. 后处理：快照 / 评测 / 截图 / 视频 / 网格
  screenshot / video / save_mesh
```

这条流水线里有一个贯穿全脚本的小机制——**scene 名映射**：`run.py` 允许你写 `--scene fox` 这种「短名」，而不是完整路径。它用一个 `get_scene()` 函数去四个字典里查表，把短名翻译成真实的数据目录。

#### 4.1.3 源码精读

参数解析集中在 `parse_args()`，注册了几十个开关。注意 `--scene` 同时是 `--training_data` 的别名，`--load_snapshot` 同时是 `--snapshot` 的别名——这是 argparse 的多别名写法：

```python
# scripts/run.py:32
parser.add_argument("--scene", "--training_data", default="", help="The scene to load. ...")
```

[scripts/run.py:27-78](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L27-L78) 用 `argparse` 注册了全部参数。这里中文要点是：每个 `add_argument` 都把一个命令行开关绑定到返回对象 `args` 的一个属性上，例如 `--n_steps` → `args.n_steps`、`--test_transforms` → `args.test_transforms`。

构造 Testbed 之后，`run.py` 立即设置 `root_dir`（让 C++ 端能找到 `configs/` 等资源），这个 `ROOT_DIR` 来自 [constants.py:21](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/constants.py#L21)：

```python
# scripts/run.py:94-95
testbed = ngp.Testbed()
testbed.root_dir = ROOT_DIR
```

scene 名映射的查表函数极其简单——遍历 `scenes.py` 里四个字典，命中即返回：

```python
# scripts/run.py:80-84
def get_scene(scene):
    for scenes in [scenes_sdf, scenes_nerf, scenes_image, scenes_volume]:
        if scene in scenes:
            return scenes[scene]
    return None
```

这四个字典定义在 [scenes.py](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/scenes.py) 里，例如 `scenes_nerf` 把短名映射到 `{data_dir, dataset, ...}` 结构。注意 `"fox"` 指向的是仓库自带的 `data/nerf/fox`（`NERF_DATA_FOLDER` 来自 [constants.py:26](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/constants.py#L26)），而 `"lego"`/`"ship"` 等指向需要你自己下载的 `nerf_synthetic` 数据集：

[scripts/scenes.py:51-74](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/scenes.py#L51-L74)：定义了 fox、lego、drums 等 NeRF 场景的短名到数据目录的映射。

拿到 `scene_info` 后，`run.py` 把 `data_dir` 和 `dataset` 拼成完整路径再调 `load_training_data`，并在该场景自带 `network` 配置时用作默认网络：

```python
# scripts/run.py:103-110
if args.scene:
    scene_info = get_scene(args.scene)
    if scene_info is not None:
        args.scene = os.path.join(scene_info["data_dir"], scene_info["dataset"])
        if not args.network and "network" in scene_info:
            args.network = scene_info["network"]
    testbed.load_training_data(args.scene)
```

这里中文要点是：**位置参数文件走 `load_file`（[scripts/run.py:97-101](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L97-L101)），`--scene` 走 `load_training_data`**。回顾 u2-l3，`load_file` 会自动判别模式；`load_training_data` 则直接按当前模式加载数据。

快照与网络配置二选一加载（`elif`），这是 u6-l3 / u2-l4 讲过的两条加载链：

[scripts/run.py:124-130](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L124-L130)：先快照后网络——有 `--load_snapshot` 就 `load_snapshot`（顺带用 `default_snapshot_filename` 把短名补全成 `base.ingp`，见 [scenes.py:226-230](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/scenes.py#L226-L230)），否则用 `--network` 调 `reload_network_from_file`。

#### 4.1.4 代码实践

**实践目标**：验证 `run.py` 的参数解析与 scene 名映射逻辑，不依赖 GPU 即可完成。

**操作步骤（源码阅读型）**：

1. 在终端运行 `python scripts/run.py --help`，对照 [scripts/run.py:27-78](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L27-L78)，把输出的参数分组：加载类（scene/snapshot/network）、训练类（n_steps/train_mode/nerf_compatibility）、评测类（test_transforms）、出图类（screenshot_*/video_*/save_mesh）。
2. 打开 [scripts/scenes.py:51-74](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/scenes.py#L51-L74)，确认 `"fox"` 这个短名会被 `get_scene("fox")` 翻译成什么路径。
3. 回答：当你执行 `python scripts/run.py fox`（fox 作为位置参数）时，数据是走 `load_file` 还是 `load_training_data`？（提示：看 [scripts/run.py:97-101](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L97-L101)）

**预期结果**：`--help` 输出的参数名、类型、默认值与源码一一对应；`fox` 经查表指向 `data/nerf/fox/`；位置参数走 `load_file`。

**说明**：本实践为纯阅读型，不涉及 GPU，可立即完成。

#### 4.1.5 小练习与答案

**练习 1**：`--scene` 和 `--load_snapshot` 都接受「短名」，它们各自用哪个函数把短名翻译成实际文件路径？

> **答案**：`--scene` 用 `get_scene(args.scene)` 拿到 `scene_info` 后，用 `os.path.join(scene_info["data_dir"], scene_info["dataset"])` 拼训练数据路径（[run.py:106](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L106)）；`--load_snapshot` 用 `default_snapshot_filename(scene_info)` 拼成 `<data_dir>/<dataset>_base.ingp` 或 `base.ingp`（[run.py:127](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L127)、[scenes.py:226-230](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/scenes.py#L226-L230)）。

**练习 2**：为什么 `--load_snapshot` 和 `--network` 用 `if/elif` 而不是两个独立的 `if`？

> **答案**：因为快照里已经包含了完整的网络配置和已训练权重（见 u6-l3），再叠加一个独立的网络配置会矛盾。脚本用 `elif` 表达「二选一」语义：有快照就只加载快照，没有才退而求其次用 `--network` 从头配置。

---

### 4.2 训练循环与 Rfl/RflRelax 调度

#### 4.2.1 概念说明

`run.py` 的核心训练循环是一个 `while testbed.frame():` 死循环，每帧训练一步。这和 GUI 模式的帧循环本质相同（u2-l2），区别只在「谁来决定何时停止」：

- **GUI 模式**：`frame()` 返回 `False` 才停（一般要关窗口），用户手动控制。
- **`run.py` 无头模式**：脚本自己数 `training_step`，到 `n_steps` 就 `break` 跳出循环。

本模块还有一个高级特性：**Rfl / RflRelax 训练调度**。instant-ngp 的 NeRF 训练有三种 `TrainMode`：`Nerf`（标准）、`Rfl`（一种正则化模式）、`RflRelax`（「表面化」放松模式）。`run.py` 允许你只在训练的某个时间段开启非标准模式，其余时间用标准 `Nerf`，从而「先用 Nerf 起步、中途切换、最后再用 Nerf 精修」。

#### 4.2.2 核心流程

训练循环每帧做四件事：

```
while testbed.frame():                          # 推进一帧（训练一步）
    ├─ 检测 UI 是否手动改了 train_mode → 若改了，关闭自动调度
    ├─ 检查 training_step >= n_steps → 到了就停（GUI 下改 shall_train）
    ├─ 进度条 t.update / set_postfix(loss)
    └─ Rfl/RflRelax 调度：按 training_step 落在哪个区间决定 train_mode
```

`n_steps` 的默认值有个重要的「智能默认」逻辑：

- 显式给了 `--n_steps N`：用 N。
- 没给且不是「加载快照后纯出图」场景：默认训练 35000 步。
- 加载了快照、且没开 GUI：默认不训练（`n_steps` 保持 -1），因为这种场景通常是「加载现成模型直接评测/出图」。

Rfl/RflRelax 的调度规则分两种（互斥，取决于初始 `--train_mode`）：

- `--train_mode rflrelax`：在 `[rflrelax_begin_step, rflrelax_end_step)` 区间内用 `RflRelax`，区间外用 `Nerf`。即「起步 Nerf → 中段 RflRelax 表面化 → 末段 Nerf 精修」。
- `--train_mode rfl`：前 `rfl_warmup_steps` 步用 `Nerf` 预热，之后切换到 `Rfl`。

#### 4.2.3 源码精读

先看「智能默认步数」。`n_steps` 初值取自 `--n_steps`（默认 -1），下面的分支决定是否覆盖为 35000：

[scripts/run.py:191-198](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L191-L198)：当 `n_steps < 0` **且**（不是「加载快照」**或**开了 GUI）时，才把默认步数设为 35000。中文要点是——「加载了快照 + 没开 GUI + 没指定步数」三元组会让 `n_steps` 保持负值，于是下面的 `if n_steps > 0` 整个训练块被跳过，脚本直接进入评测/出图阶段。

训练循环本体：[scripts/run.py:205-251](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L205-L251)。这里中文逐段说明：

- `while testbed.frame()`：每帧训练并（若有窗口）渲染一步，对应 pyngp 的 `frame` 绑定 [python_api.cu:504-506](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L504-L506)，注意它用了 `call_guard<gil_scoped_release>()` 在执行期间释放 GIL（u7-l1 讲过这点）。
- 停止判定（[run.py:216-220](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L216-L220)）：`training_step >= n_steps` 时，GUI 下只关训练（`shall_train = False`，窗口继续开着），无头下直接 `break`。

最关键的 Rfl/RflRelax 调度段：

```python
# scripts/run.py:227-242
# Rfl-relax training schedule
if use_training_schedule:
    if original_train_mode == ngp.TrainMode.RflRelax:
        if args.rflrelax_begin_step <= testbed.training_step < args.rflrelax_end_step:
            testbed.nerf.training.train_mode = ngp.TrainMode.RflRelax
        else:
            testbed.nerf.training.train_mode = ngp.TrainMode.Nerf
    elif original_train_mode == ngp.TrainMode.Rfl:
        if testbed.training_step > args.rfl_warmup_steps:
            testbed.nerf.training.train_mode = ngp.TrainMode.Rfl
        else:
            testbed.nerf.training.train_mode = ngp.TrainMode.Nerf
```

中文要点：调度是**逐帧**执行的——每一帧根据当前 `training_step` 重新决定 `train_mode`。`RflRelax` 用一个**闭区间** `[begin, end)`（默认 `[15000, 30000)`，参数见 [run.py:46-47](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L46-L47)）；`Rfl` 用一个**阈值**（默认 warmup 1000 步，参数 [run.py:45](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L45)）。`TrainMode` 枚举本身在 C++ 端定义，经 pyngp 导出为 `ngp.TrainMode.{Nerf,Rfl,RflRelax}`：[python_api.cu:311-315](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L311-L315)。

还有一个保护逻辑：如果用户在 GUI 里手动切换了 `train_mode`（`prev_train_mode != testbed.nerf.training.train_mode`），脚本就关闭自动调度（`use_training_schedule = False`），避免脚本和用户抢控制权：[run.py:208-210](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L208-L210)。

#### 4.2.4 代码实践

**实践目标**：理解 `n_steps` 的智能默认与 Rfl/RflRelax 调度的切换时刻。

**操作步骤（源码阅读型）**：

1. 假设你执行 `python scripts/run.py fox`（不指定 `--n_steps`，不加载快照，不开 GUI）。读 [run.py:191-198](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L191-L198)，判断 `n_steps` 最终会是多少。
2. 假设你执行 `python scripts/run.py --load_snapshot fox.ingp --test_transforms ...`（加载快照做评测，不开 GUI）。判断训练循环会不会执行？为什么？
3. 假设 `--train_mode rflrelax --rflrelax_begin_step 15000 --rflrelax_end_step 30000 --n_steps 35000`，列出 `training_step` 在 `0 / 14999 / 15000 / 29999 / 30000 / 34999` 这几个时刻各自的 `train_mode`。

**预期结果**：

1. `n_steps = 35000`（因为不是「快照+无GUI」场景）。
2. 不会执行——`n_steps` 保持 -1，`if n_steps > 0` 为假，训练块跳过，这正是「加载现成模型直接评测」的预期行为。
3. 各时刻 `train_mode`：

| training_step | train_mode | 原因 |
| --- | --- | --- |
| 0 | Nerf | 0 < 15000，区间外 |
| 14999 | Nerf | 14999 < 15000，区间外 |
| 15000 | RflRelax | 15000 ≥ 15000 且 < 30000，区间内 |
| 29999 | RflRelax | 仍在区间内 |
| 30000 | Nerf | 30000 不再 < 30000，区间外 |
| 34999 | Nerf | 区间外 |

**说明**：纯逻辑推演，无需 GPU。若要在真实 GPU 上验证，可训练 `fox` 并在第 15000 步前后打印 `testbed.nerf.training.train_mode`。

#### 4.2.5 小练习与答案

**练习 1**：`original_train_mode` 和 `prev_train_mode` 这两个变量分别记录什么？为什么需要区分？

> **答案**：`original_train_mode`（[run.py:200](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L200)）记录的是**脚本启动时**用户通过 `--train_mode` 设定的初始模式，整个运行周期不变，用来决定走哪条调度分支。`prev_train_mode`（[run.py:201](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L201)）记录**上一帧**的模式，每帧末尾更新（[run.py:251](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L251)），用来检测用户是否在 GUI 里手动改了模式。

**练习 2**：为什么 RflRelax 的区间用左闭右开 `[begin, end)` 而写成 `begin <= step < end`，而 Rfl 用 `step > warmup` 严格大于？

> **答案**：这是作者的设计选择，目的是让两个区间边界恰好衔接——RflRelax 在 `end_step` 那一帧立刻切回 Nerf；Rfl 在 warmup 之后的第 `warmup+1` 帧才切 Rfl，留出一个完整的预热周期。严格大于保证 warmup 这一步本身仍属 Nerf。

---

### 4.3 --test_transforms：PSNR/SSIM 评测

#### 4.3.1 概念说明

训练完一个 NeRF，科研里最关心的问题是：「它在新视角下渲染得有多准？」这需要一组**训练时没见过的测试视角**（test transforms），逐张渲染并与真实照片比指标。

`run.py` 的 `--test_transforms` 就是这条评测链。它对测试集里每张图：把相机摆到对应的训练视角、渲染出图、与该视角的真值图计算误差，最后汇总成 PSNR 和 SSIM。

两个指标的定义：

- **MSE**（均方误差）：\(\mathrm{MSE} = \frac{1}{N}\sum (A_i - R_i)^2\)，其中 \(A\) 是渲染图、\(R\) 是真值图。
- **PSNR**（峰值信噪比）：\(\mathrm{PSNR} = 10\cdot \log_{10}\!\left(\frac{1}{\mathrm{MSE}}\right)\)（这里像素峰值取 1）。PSNR 越高越好，NeRF 论文里合成场景通常报 30+ dB。
- **SSIM**（结构相似性）：在亮度、对比度、结构三方面衡量相似度，取值 \([0,1]\)，越接近 1 越好。

#### 4.3.2 核心流程

```
读 test_transforms.json
  ↓
关训练、关背景透明、开像素中心对齐、提高 render_min_transmittance 精度
  ↓
load_training_data(test_transforms)   # 把测试集当作"数据"加载
  ↓
for i in 每张测试图:
    set_camera_to_training_view(i)    # 摆到第 i 个视角
    ref = render(spp=1, ground_truth=True)   # 真值图
    img = render(spp=8)                       # 模型渲染图
    MSE = compute_error("MSE", srgb(img), srgb(ref))
    SSIM = compute_error("SSIM", ...)
    PSNR = mse2psnr(MSE)
累加 → 求平均 → 打印 "PSNR=... SSIM=..."
```

值得注意的设计：评测时把**测试集**当作训练数据 `load_training_data` 进去，但 `shall_train = False` 不训练，只为了用 `set_camera_to_training_view(i)` 拿到每个测试视角的相机参数，并用 `render_ground_truth=True` 直接取该视角的真实照片作为参考图。

#### 4.3.3 源码精读

评测开始前有一串「评测专用配置」，目的是让指标与既往 NeRF 论文可比：

[scripts/run.py:269-282](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L269-L282)。中文逐条说明：

- `background_color = [0,0,0,1]`：黑背景，避免 alpha 混入干扰。
- `snap_to_pixel_centers = True`：关掉抗锯齿，像素吸附到中心——既往 NeRF 论文不做 MSAA，为了可比也关掉。
- `spp = 8`：每像素采样 8 次（评测用比截图少，够稳即可）。
- `render_min_transmittance = 1e-4`：放宽光线提前终止阈值，让远处的薄雾也被算进来，提高精度（回顾 u4-l3，默认是 0.01）。
- `render_with_lens_distortion = True`：按镜头畸变渲染，匹配真实相机。
- `load_training_data(test_transforms)`：把测试集载入，使 `set_camera_to_training_view` 能索引到各测试视角。

逐图渲染与指标计算的循环：

[scripts/run.py:284-312](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L284-L312)。中文要点：

- `set_camera_to_training_view(i)`：把相机摆到测试集第 `i` 张图的位姿（pyngp 绑定见 [python_api.cu:654](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L654)）。
- `render(resolution[0], resolution[1], 1, True)`：先以 `spp=1` + `render_ground_truth=True` 渲染**真值图** `ref_image`；`render` 的签名见 [python_api.cu:507-519](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L507-L519)（参数顺序是 width, height, spp, linear, start_t, end_t, fps, shutter_fraction）。
- `render(resolution[0], resolution[1], spp, True)`：再以 `spp=8` 渲染**模型预测图** `image`。
- 第一张图额外写出 `ref.png` / `out.png` / `diff.png` 三张对照图（[run.py:293-299](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L293-L299)），方便人眼检查。

指标计算本身：先把两图都经 `linear_to_srgb` 转 sRGB 再 clip 到 \([0,1]\)，然后调 `compute_error`：

```python
# scripts/run.py:301-307
A = np.clip(linear_to_srgb(image[...,:3]), 0.0, 1.0)
R = np.clip(linear_to_srgb(ref_image[...,:3]), 0.0, 1.0)
mse = float(compute_error("MSE", A, R))
ssim = float(compute_error("SSIM", A, R))
...
psnr = mse2psnr(mse)
```

`compute_error` 是个通用指标分发器，按字符串名选公式（MSE / SSIM / FLIP / …），定义在 [common.py:249-255](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/common.py#L249-L255)，内部先算逐像素误差图 `compute_error_img`（[common.py:212-247](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/common.py#L212-L247)）再求均值。PSNR 的换算就一行：

```python
# scripts/common.py:35
def mse2psnr(x): return -10.*np.log(x)/np.log(10.)
```

中文要点：`-10·ln(x)/ln(10) = -10·log10(x) = 10·log10(1/x)`，与上面 PSNR 定义一致。

最后汇总并打印：

[scripts/run.py:314-317](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L314-L317)：算出 `psnr_avgmse`（先平均 MSE 再换算 PSNR）和 `psnr`（先逐图换算再平均）两种口径，打印 `PSNR=... [min=... max=...] SSIM=...`。中文要点是——两种 PSNR 口径数值略有差异，科研报告中要说明用的是哪一种。

#### 4.3.4 代码实践

**实践目标**：用 `--test_transforms` 对一个带测试集的 NeRF 数据集做评测，记录 PSNR/SSIM。

**操作步骤（需 GPU，本机若无则改阅读型）**：

1. 准备一个训练集与测试集分离的数据集（如 `nerf_synthetic` 的 `transforms_train.json` / `transforms_test.json`），或用自带 fox 的 `transforms.json`（fox 的训练/测试是同一份）。
2. 训练并评测：
   ```bash
   python scripts/run.py \
     --scene /path/to/lego \
     --test_transforms /path/to/lego/transforms_test.json \
     --n_steps 35000
   ```
   `--n_steps` 不给也会默认 35000；训练完后脚本自动进入评测。
3. 观察终端最后输出的一行 `PSNR=... [min=... max=...] SSIM=...`，以及目录下生成的 `ref.png` / `out.png` / `diff.png`。

**需要观察的现象**：

- 训练阶段 `tqdm` 进度条的 loss 随步数下降。
- 评测阶段进度条切换为 `Rendering test frame`，`psnr` 字段逐图累积上升。
- 最终 `PSNR` 数值（合成 lego 通常 35+ dB），`SSIM` 接近 0.98。

**预期结果**：终端打印形如 `PSNR=35.xx [min=33.xx max=37.xx] SSIM=0.97xx`。`diff.png` 中差异越暗说明渲染越准。

**待本地验证**：具体 PSNR/SSIM 数值依赖 GPU 与数据，请以本地实测为准；无 GPU 时可只做源码阅读部分。

#### 4.3.5 小练习与答案

**练习 1**：为什么评测时要把 `render_min_transmittance` 从默认的 0.01 调成 1e-4？

> **答案**：`render_min_transmittance` 是光线提前终止的阈值——剩余透射率低于它就不再采样（u4-l3）。默认 0.01 会略早地砍掉远处贡献，渲染更快但精度略低；评测追求精度，故放宽到 1e-4，让远处的薄雾/半透明也被算入，使 PSNR 更准。

**练习 2**：`compute_error` 计算前为什么要先 `linear_to_srgb` 再 `np.clip(..., 0, 1)`？

> **答案**：pyngp 的 `render(..., linear=True)` 返回的是**线性**空间的颜色（内部计算空间），而 PSNR/SSIM 的既往报告惯例是在 **sRGB** 空间比较。先转 sRGB 保证与论文口径一致；再 clip 到 \([0,1]\) 是因为 sRGB 的合法范围就是 \([0,1]\)，超出部分（如高光溢出）截断后才能正确量化比较。

**练习 3**：`psnr` 与 `psnr_avgmse` 两种口径在数学上有何区别？

> **答案**：`psnr` 是「逐图算 PSNR 再求平均」\(\frac{1}{N}\sum_i 10\log_{10}(1/\mathrm{MSE}_i)\)；`psnr_avgmse` 是「先把所有图 MSE 求平均再换算 PSNR」\(10\log_{10}(1/\overline{\mathrm{MSE}})\)。由于对数是凹函数，由 Jensen 不等式有 `psnr >= psnr_avgmse` 不一定成立方向（取决于方差），两者通常不同，报告需注明。

---

### 4.4 批量截图与视频渲染

#### 4.4.1 概念说明

评测回答「准不准」，出图回答「好不好看」。`run.py` 提供两种「把训练好的 NeRF 渲染成图片」的途径：

- **批量截图**（`--screenshot_transforms` / `--screenshot_dir`）：按一组相机位姿（一份 `transforms.json`）逐张渲染并保存为 PNG，用于科研里的定性对比图。
- **视频**（`--video_camera_path`）：加载一条相机路径（关键帧 + 样条，见 u6-l2），按帧率逐帧渲染，再用 ffmpeg 拼成 mp4，用于出电影感运镜视频。

两者的底层都是同一个 `testbed.render()`，区别在「相机位姿从哪来」和「结果是单图还是视频」。

#### 4.4.2 核心流程

**批量截图**（按 transforms）：

```
读 screenshot_transforms.json → ref_transforms
设置 fov（从 camera_angle_x 推导）
for idx in screenshot_frames:
    cam_matrix = frames[idx]["transform_matrix"]
    set_nerf_camera_matrix(cam_matrix)      # 摆相机
    image = render(w, h, screenshot_spp)
    write_image(输出路径, image)
```

**视频**（按 camera path）：

```
load_camera_path(video_camera_path)
n_frames = video_n_seconds * video_fps
for i in range(n_frames):
    camera_smoothing = video_camera_smoothing
    # 关键：用 [i/n, (i+1)/n] 时间区间驱动相机路径插值 + 运动模糊
    frame = render(w, h, video_spp, True, i/n, (i+1)/n, video_fps, shutter_fraction=0.5)
    write_image(tmp/i.jpg, frame * 2**exposure)
ffmpeg 把 tmp/*.jpg 拼成 video.mp4
```

视频渲染有一个细节：`render` 的 `start_t`/`end_t` 参数把「第 i 帧对应的归一化时间区间」传给底层，由相机路径在该区间内插值出相机位姿，并配合 `shutter_fraction` 模拟运动模糊（回顾 u6-l2）。

#### 4.4.3 源码精读

**批量截图**分支：[scripts/run.py:325-352](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L325-L352)。中文要点：

- 先从 `ref_transforms["camera_angle_x"]` 把弧度焦距转成度数设给 `testbed.fov`（[run.py:327-328](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L327-L328)）。
- `screenshot_frames` 默认是全部帧（[run.py:329-330](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L329-L330)），也可用 `--screenshot_frames` 指定子集。
- 每帧取 `transform_matrix`（兼容 `transform_matrix_start`），用 `set_nerf_camera_matrix` 摆相机（[run.py:335-342](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L335-L342)），渲染并 `write_image`（[run.py:349-352](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L349-L352)）。

没有 transforms 时，`--screenshot_dir` 退化为只渲染当前视角一张图（[run.py:353-359](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L353-L359)）。

**视频**分支：[scripts/run.py:361-395](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L361-L395)。中文逐段说明：

- `load_camera_path` 加载相机路径（u6-l2 讲过的关键帧 + 样条），绑定见 [python_api.cu:572](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L572)。
- `n_frames = video_n_seconds * video_fps`：总帧数 = 时长 × 帧率。
- `save_frames = "%" in video_output`：若输出名里含 `%`（如 `video_%04d.png`），就逐帧存图不合成视频；否则合成 mp4。
- 渲染调用的关键参数是时间区间：

```python
# scripts/run.py:386
frame = testbed.render(resolution[0], resolution[1], args.video_spp, True,
                       float(i)/n_frames, float(i + 1)/n_frames,
                       args.video_fps, shutter_fraction=0.5)
```

中文要点：第 5、6 个参数是 `start_t=i/n_frames`、`end_t=(i+1)/n_frames`，把「这一帧在整条路径上的归一化时间区间」交给底层；底层在该区间内由相机路径插值出位姿，并用 `shutter_fraction=0.5`（半帧快门）模拟运动模糊。

- `--video_render_range` 有个巧妙的「前导热身」机制（[run.py:376-382](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L376-L382)）：当你只想渲染路径的一段 `[start_frame, end_frame]` 时，脚本不能直接从中间开始渲染——因为 `camera_smoothing`（EMA 轨迹平滑，u6-l2）是滞后的，必须从第 0 帧开始跑才能让平滑滤波器进入正确状态。所以 `start_frame` 之前的帧用 `32×32` 的极小图渲染后丢弃（「热身」），代价是浪费一点时间换取正确的相机平滑。

- 合成：把每帧 `write_image` 成 `tmp/0000.jpg` 等，再 `os.system` 调 ffmpeg：[run.py:388-393](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L388-L393)。中文要点是 `frame * 2**exposure` 把 `--exposure` 的曝光偏移（以 2 的幂）应用到像素亮度上。

图像写出统一走 `write_image`（[common.py:149-164](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/common.py#L149-L164)），它按扩展名分流：`.bin` 走自定义半精度格式，其余走 imageio，并负责线性→sRGB 转换与 alpha 反预乘。

#### 4.4.4 代码实践

**实践目标**：用 `--video_camera_path` 从一个训练好的 fox 渲染一段 mp4 视频（或退化为阅读型）。

**操作步骤（需 GPU + ffmpeg）**：

1. 先训练 fox 并存快照：
   ```bash
   python scripts/run.py --scene fox --n_steps 20000 --save_snapshot fox.ingp
   ```
2. 加载快照渲染视频（假设你有一条相机路径 `base_cam.json`，可在 GUI 里用 Camera path 面板录制后导出）：
   ```bash
   python scripts/run.py \
     --load_snapshot fox.ingp \
     --video_camera_path base_cam.json \
     --video_n_seconds 3 --video_fps 30 --video_spp 8 \
     --video_output fox.mp4
   ```
3. 观察终端：进度条 `Rendering video` 逐帧推进，结束后生成 `fox.mp4`。

**需要观察的现象**：

- 因为加载了快照、没开 GUI、没给 `--n_steps`，训练循环被跳过（回顾 4.2 的智能默认），脚本直接进入视频渲染。
- 若设置了 `--video_render_range 10 30`，前 10 帧会以 `32×32` 小图「空跑」（热身），真正的输出帧从第 10 帧开始。

**预期结果**：生成 `fox.mp4`，时长约 3 秒、30 fps、共 90 帧。运动模糊在快速转动相机时可见。

**待本地验证**：相机路径文件需自行准备；ffmpeg 需在 PATH 中。无 GPU 时请改做源码阅读：解释 [run.py:376-382](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L376-L382) 为何要渲染丢弃小图。

#### 4.4.5 小练习与答案

**练习 1**：批量截图用 `set_nerf_camera_matrix` 摆相机，而视频用 `render(..., start_t, end_t, ...)` 隐式驱动相机。为什么视频不能也用 `set_nerf_camera_matrix`？

> **答案**：因为视频的相机位姿来自**相机路径的样条插值**（u6-l2），且需要在一帧的时间区间内支持 `shutter_fraction` 运动模糊和 `camera_smoothing` 平滑——这些都需要底层在 `[start_t, end_t]` 区间内自己求值路径，而不是外部一次性给定单个位姿矩阵。`render` 的 `start_t/end_t/fps/shutter_fraction` 参数就是为这条「路径驱动渲染」链设计的（[python_api.cu:507-519](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L507-L519)）。

**练习 2**：`--video_render_range` 的 START_FRAME 之前为何要渲染 `32×32` 的小图再丢弃？

> **答案**：`camera_smoothing` 是一个指数移动平均（EMA）低通滤波器，状态依赖于历史帧。若直接从 START_FRAME 开始渲染，EMA 滤波器还没有「热身」，相机轨迹会偏离正确位置。因此脚本从第 0 帧开始跑，但 START_FRAME 之前的帧用极小的 32×32 图渲染（几乎不耗时）只为喂给平滑滤波器，真正的高质量渲染只对 `[START_FRAME, END_FRAME]` 进行。这正是 u6-l2 提到的「EMA 滞后导致端点不可达」的工程对策。

---

## 5. 综合实践

**综合任务**：把本讲四个模块串起来，用 `run.py` 完成一次「训练 → 评测 → 出图 → 出视频」的完整科研工作流，并用一句话解释每一步用了哪个 `run.py` 参数。

**操作步骤（需 GPU + ffmpeg）**：

1. **训练并评测**（用到 4.1、4.2、4.3）：
   ```bash
   python scripts/run.py \
     --scene fox \
     --n_steps 20000 \
     --test_transforms data/nerf/fox/transforms.json \
     --save_snapshot fox.ingp
   ```
   记录终端最后打印的 `PSNR=... SSIM=...`，并查看生成的 `ref.png` / `out.png` / `diff.png`。

2. **加载快照批量截图**（用到 4.4）：
   ```bash
   python scripts/run.py \
     --load_snapshot fox.ingp \
     --screenshot_transforms data/nerf/fox/transforms.json \
     --screenshot_frames 0 10 20 \
     --screenshot_dir shots
   ```
   观察：因为加载了快照、无 GUI、无 `--n_steps`，训练被跳过（4.2 的智能默认），脚本直接渲染第 0、10、20 个视角到 `shots/`。

3. **加载快照出视频**（用到 4.4）：
   ```bash
   python scripts/run.py \
     --load_snapshot fox.ingp \
     --video_camera_path base_cam.json \
     --video_n_seconds 2 --video_fps 30 \
     --video_output fox.mp4
   ```

4. **RflRelax 对照实验**（用到 4.2）：再训练一次但用 RflRelax 调度，对比 PSNR：
   ```bash
   python scripts/run.py \
     --scene fox \
     --n_steps 35000 \
     --train_mode rflrelax \
     --rflrelax_begin_step 15000 --rflrelax_end_step 30000 \
     --test_transforms data/nerf/fox/transforms.json
   ```
   观察终端是否打印 `Disabling Rfl/RflRelax training schedule...`（不应该出现，因为没动 GUI），并对比 PSNR 与步骤 1 的差异。

**预期结果**：步骤 1 给出基准 PSNR/SSIM 与三张对照图；步骤 2 产出 3 张指定视角截图；步骤 3 产出 `fox.mp4`；步骤 4 验证 RflRelax 在 `[15000,30000)` 区间生效（可在 GPU 上打印 `train_mode` 确认）。

**待本地验证**：PSNR/SSIM 数值与视频质量依赖 GPU、数据与相机路径，请以本地实测为准。无 GPU 环境可只完成「读 `run.py` 源码、解释每个参数对应哪个模块」的阅读部分。

## 6. 本讲小结

- `scripts/run.py` 是 pyngp 的官方「脚本脚手架」，用 `argparse` 把几十个命令行开关映射成对 `Testbed` 的调用，复刻并扩展了 `instant-ngp` 命令行的全部能力。
- **scene 名映射**：`get_scene()` 在 `scenes.py` 的四个字典里把短名（如 `fox`）翻译成真实数据目录；位置参数走 `load_file`、`--scene` 走 `load_training_data`，`--load_snapshot`/`--network` 二选一加载。
- **训练循环**：`while testbed.frame():` 推进帧，到 `n_steps` 停；`n_steps` 有「加载快照+无GUI」时不训练的智能默认（默认 35000 步，纯评测场景为 -1 跳过训练）。
- **Rfl/RflRelax 调度**：逐帧按 `training_step` 落在哪个区间决定 `train_mode`——RflRelax 在 `[begin, end)` 区间内启用、区间外回 Nerf；Rfl 在 warmup 之后切换；用户手动改模式则关闭自动调度。
- **--test_transforms 评测**：把测试集当作数据载入但不训练，逐视角 `set_camera_to_training_view` + `render` 出预测图与真值图，算 MSE/SSIM 并经 `mse2psnr` 换算 PSNR，打印 `PSNR=... [min max] SSIM=...`。
- **批量截图与视频**：截图按 `transforms.json` 用 `set_nerf_camera_matrix` 逐张渲染；视频按 `camera_path` 用 `render(..., start_t, end_t, fps, shutter_fraction)` 路径驱动渲染再 ffmpeg 合成，`--video_render_range` 用 32×32 小图热身以校正 EMA 相机平滑。

## 7. 下一步学习建议

- **想深挖评测指标本身**：阅读 [common.py:212-255](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/common.py#L212-L255) 的 `compute_error_img` / `compute_error`，它支持 MSE、SSIM、FLIP 等多种指标，可作为指标实现的参考。
- **想自动化准备数据集**：进入 [u7-l3 数据集准备脚本](u7-l3-dataset-prep-scripts.md)，学习 `colmap2nerf.py` 如何从一组照片生成 `transforms.json`，从而喂给本讲的 `run.py`。
- **想理解相机路径与运动模糊的底层**：回到 [u6-l2 相机路径与视频渲染](u6-l2-camera-path-and-video.md)，对照 [src/camera_path.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/camera_path.cu) 看 `render(start_t, end_t)` 背后的样条求值与快门模拟。
- **想扩展 run.py 做自己的实验**：建议先通读本讲四个模块的源码链接，再仿照 4.3 的评测段写一个自定义指标（如 FLIP）批处理脚本——这正是 u8-l5「扩展 instant-ngp」会涉及的二次开发范式。
