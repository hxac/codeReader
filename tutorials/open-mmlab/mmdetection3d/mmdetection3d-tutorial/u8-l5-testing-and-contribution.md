# 测试体系与贡献流程

## 1. 本讲目标

学完本讲，你应该能够：

1. 按 `tests/` 目录的镜像规则，在 30 秒内定位任意模块（数据集、检测头、损失函数）对应的测试文件。
2. 说出 `mmdet3d/testing` 提供的伪数据工厂（`create_dummy_data_info`、`create_detector_inputs`、`get_detector_cfg` 等）各自解决什么问题、如何配合使用。
3. 区分仓库里三种测试写法：裸函数式、`unittest.TestCase` 类式、以及「构建真实模型 + 伪输入」的冒烟测试，并为自己的自定义模块选对范式。
4. 读懂 `.circleci/` 两级 CI 配置：路径过滤决定「只查风格还是全量测试」，测试矩阵覆盖最低/最高版本的 CPU 与 GPU 环境。
5. 按照 `docs/en/notes/contribution_guides.md` 的六步流程提交一个符合规范的 PR，并用 pre-commit 在本地提前拦截风格问题。

本讲是整个手册最后一篇「工程视角」讲义：前面七个子单元讲的是「代码怎么写」，这一讲讲「怎么证明代码没写错，以及怎么把代码交出去」。

## 2. 前置知识

- **单元测试（unit test）**：对最小可测单元（一个函数、一个类）自动验证「给定输入 → 期望输出」。Python 生态标准工具是 `pytest`，它既能跑裸函数 `test_xxx()`，也能跑标准库 `unittest.TestCase` 的类。
- **冒烟测试（smoke test）**：不求覆盖所有分支，只求「完整跑一遍不炸」。MMDetection3D 的模型测试大多属于这一类——用随机输入前向一次，断言 loss 非负、输出字段齐全。
- **断言（assert）**：`assert cond, msg` 在 `cond` 为假时抛 `AssertionError`。pytest 会把这条异常报告为「测试失败」而非「程序崩溃」。
- **确定性（可复现性）**：随机输入若不固定种子，测试结果会抖动。`setup_seed` 一次性固定 `random`、`numpy`、`torch`（含 CUDA）三套随机源。
- **pre-commit**：在 `git commit` 前自动执行的钩子集合，本仓库用它跑 flake8（语法风格）、isort（导入排序）、yapf（格式化）、codespell（拼写）、mdformat（Markdown 格式）等十余个检查器。
- **CircleCI 路径过滤（path-filtering）**：只改了文档的 PR 不必跑全量 GPU 测试——CI 先用正则匹配改动文件，再决定触发哪条工作流。
- 你应该已经完成 [u4-l1 注册器机制]、[u3-l8 Pack3DDetInputs] 与 [u7-l1 训练全流程]：本讲的伪输入工厂产出的正是 Pack3DDetInputs 的下游契约，而 `MODELS.build` 与 `DefaultScope` 正是注册器讲义的内容。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| `tests/` | 测试根目录，九个子目录镜像 `mmdet3d/` 包结构 |
| `tests/data/` | 内置迷你数据集（KITTI 单帧、nuScenes 单帧等），使测试无需下载真实数据 |
| `tests/test_datasets/test_kitti_dataset.py` | 函数式测试范例：真实读 pkl + 断言解析结果 |
| `tests/test_structures/test_det3d_data_sample.py` | 类式测试范例：纯随机张量验证数据契约 |
| `tests/test_models/test_detectors/test_voxelnet.py` | 冒烟测试范例：真实配置建模型 + 伪输入前向 |
| `mmdet3d/testing/` | 伪数据工厂（`data_utils.py` + `model_utils.py`），被所有模型测试复用 |
| `.circleci/config.yml` | CI 第一级：路径过滤，计算 `lint_only` 参数 |
| `.circleci/test.yml` | CI 第二级：lint / build_cpu / build_cuda 三类任务与工作流编排 |
| `docs/en/notes/contribution_guides.md` | 官方贡献指南：fork → 分支 → pre-commit → PR 六步流程 |
| `docs/en/notes/changelog.md` | 版本变更日志，PR 合并后由维护者汇总 |
| `requirements/tests.txt` | 测试专用依赖（pytest、coverage、xdoctest 等） |
| `.pre-commit-config.yaml` / `setup.cfg` | 风格检查规则（flake8、isort、yapf、codespell 的参数） |

## 4. 核心概念与源码讲解

### 4.1 tests/ 目录组织与迷你数据集

#### 4.1.1 概念说明

大型项目测试的第一问题不是「怎么写断言」，而是「测试放在哪、数据从哪来」。MMDetection3D 的答案是**镜像原则**：`tests/` 的子目录名与 `mmdet3d/` 包的子目录一一对应，找测试就是做字符串替换——`mmdet3d/datasets/` 的测试在 `tests/test_datasets/`，`mmdet3d/models/detectors/` 的测试在 `tests/test_models/test_detectors/`。

第二个问题是数据。3D 检测数据集动辄几十 GB，单元测试不可能依赖它们。仓库的解法是把**最小可用的真实样本**直接提交进 git：`tests/data/kitti/` 里只有一个 000000 帧（点云、图像、标定各一份）加上提前生成好的 `kitti_infos_train.pkl`（只含这一帧的 info）。测试读到的 pkl 结构与生产环境完全一致，只是规模缩到 1 条。

#### 4.1.2 核心流程

定位某个模块测试的流程：

```text
想测 mmdet3d/<子包>/<模块>.py
  ↓ 把 mmdet3d/ 替换成 tests/，并在包名前加 test_
得到 tests/test_<子包>/test_<模块名>.py
  ↓ 若不存在，再按「类别目录」降级查找
得到 tests/test_<类别>/test_<模块>.py（如 test_models/test_detectors/）
```

测试数据的两个事实：

- `tests/data/kitti/training/velodyne_reduced/` 只有 `000000.bin` 一个文件——这正是 [u3-l2] 讲过的「velodyne_reduced 是训练配置硬依赖」的测试版缩影。
- `tests/data/` 下还有 `nuscenes/`、`scannet/`、`sunrgbd/`、`s3dis/`、`semantickitti/`、`waymo/`、`lyft/`、`ops/` 八个兄弟目录，各自只装单帧级数据。

#### 4.1.3 源码精读

测试根目录的九个子目录（命令 `ls tests/` 的真实输出）：

```text
data  test_apis  test_datasets  test_engine  test_evaluation
test_models  test_samples  test_structures  test_utils
```

对照 `mmdet3d/` 包的 `datasets/`、`models/`、`structures/`、`engine/`、`evaluation/`、`apis/` 六大子模块（见 [u1-l4 源码地图]），镜像关系一目了然。两个额外目录：`test_samples/` 存放二进制采样输入（如 `parta2_roihead_inputs.npz`），`test_utils/` 测试包级工具函数。

迷你 KITTI 数据集的内容（目录 `tests/data/kitti/`，无单独源码文件，目录树见本地 `ls`）：`kitti_infos_train.pkl` 是数据集构造的唯一入口，`kitti_dbinfos_train.pkl` 与 `kitti_gt_database/` 服务于 GT 采样测试（呼应 [u3-l7]），`kitti_infos_mono3d.pkl` 与两个 `mono3d_sample_results.pkl` 服务于单目评估测试（呼应 [u7-l2]）。一份数据被多组测试复用，是「迷你数据集」设计的核心收益。

测试专用依赖清单在 [requirements/tests.txt:L1-L12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/requirements/tests.txt#L1-L12)：核心是 `pytest`、`pytest-cov`（覆盖率）、`coverage`，另有 `xdoctest`（文档字符串里的示例可执行验证）与风格工具。CI 装 runtime 依赖之外会额外 `pip install -r requirements/tests.txt`。

#### 4.1.4 代码实践

1. **实践目标**：建立「模块 → 测试文件」的快速映射能力。
2. **操作步骤**：
   - 不看上文，独立写出 `mmdet3d/models/dense_heads/anchor3d_head.py`、`mmdet3d/datasets/transforms/loading.py`、`mmdet3d/structures/bbox_3d/lidar_box3d.py` 三个文件对应的候选测试路径；
   - 用 `ls tests/test_models/test_dense_heads/ tests/test_datasets/test_transforms/ tests/test_structures/` 验证你的猜测。
3. **需要观察的现象**：绝大多数文件名能按 `test_` 前缀规则直接命中；少数（如检测器）按类别目录组织。
4. **预期结果**：能命中 2 个以上；对未命中的能说明它落在哪个类别目录。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么测试数据要提交进仓库，而不是像训练那样从官网下载？

**答案**：单元测试要求数据**恒定可得**且**体量极小**。下载依赖网络与外网稳定性，会让 CI 与新贡献者的本地测试随机失败；单帧迷你数据既能触发完整真实代码路径（读 pkl、解析标定、坐标转换），又只有几 MB。真实训练数据与测试数据的取舍标准不同：前者要分布覆盖，后者要路径覆盖。

**练习 2**：`tests/data/kitti/` 里的 `kitti_gt_database/` 目录服务于哪条 [u3-l7] 讲过的机制？为什么它的测试也需要真实点云？

**答案**：服务于 GT 数据库采样（ObjectSample / DataBaseSampler）。GT 数据库存的是「以框中心为原点的相对坐标点云 `.bin`」，采样后要贴回当前场景并参与碰撞测试——这些操作都消费真实点张量，用随机点也可以跑通形状，但仓库选择复用真实帧以保证 `num_points_in_gt` 等过滤逻辑走真实分支。

### 4.2 mmdet3d/testing：伪数据工厂

#### 4.2.1 概念说明

模型测试的共同痛点是：模型的输入不是普通张量，而是 [u3-l8] 讲过的 `dict(inputs=..., data_samples=[Det3DDataSample])` 复杂契约——`data_samples` 里要装 `gt_instances_3d`（含正确坐标系的 3D 框对象）、`gt_instances`、`gt_pts_seg`，还要带 `box_type_3d`、`lidar2img` 等元信息。如果每个测试文件手搓一遍，会重复上百行且容易漂移。

`mmdet3d/testing/` 就是官方提炼的**伪数据工厂**，两个文件分工明确：

- `data_utils.py`：制造「数据管线级」伪数据——一条完整的 info 字典（含标定矩阵、图像路径、实例标注），用于测数据集类与 transforms。
- `model_utils.py`：制造「模型级」伪输入——打包好的 batch dict、从真实 config 抽出的模型配置、全局种子固定。

它位于 `mmdet3d/` 包内而非 `tests/` 下，意味着**随包发布**：你 pip 安装 mmdet3d 后同样可以 `from mmdet3d.testing import create_detector_inputs` 来测自己的模型——这是官方给二次开发者的正式赠品。

#### 4.2.2 核心流程

一次典型模型测试的组装流水线：

```text
setup_seed(0)                          # 固定三套随机源
       ↓
get_detector_cfg('configs/相对路径')     # 读真实 config，深拷贝 model，
       ↓                               # 并把 train_cfg/test_cfg 提升进 model
MODELS.build(cfg)                      # 注册器建出真实模型（呼应 u4-l1）
       ↓
create_detector_inputs(num_gt_instance=2, ...)
       # 产出 dict(inputs={'points': [...]},
       #           data_samples=[Det3DDataSample])   ← Pack3DDetInputs 的下游契约
       ↓
data = model.data_preprocessor(packed_inputs, True)
model.forward(**data, mode='predict' / 'loss')
```

`create_detector_inputs` 内部的关键决策由开关参数控制：`with_points`/`with_img` 决定模态；`bboxes_3d_type` 取 `'lidar'/'depth'/'cam'` 决定框子类（室内检测器要 depth）；`with_pts_semantic_mask` 等开关决定分割监督字段是否生成。

#### 4.2.3 源码精读

工厂的公共接口共七个，见 [mmdet3d/testing/\_\_init\_\_.py:L2-L12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/testing/__init__.py#L2-L12)：数据侧三个（`create_dummy_data_info`、`create_data_info_after_loading`、`create_mono3d_data_info_after_loading`）与模型侧四个（`create_detector_inputs`、`get_detector_cfg`、`get_model_cfg`、`setup_seed`）。

`setup_seed` 一次固定四套随机源并启用确定性卷积，见 [mmdet3d/testing/model_utils.py:L15-L20](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/testing/model_utils.py#L15-L20)。注意它连 `torch.backends.cudnn.deterministic` 都设了——卷积加速算法的非确定性也被关掉，保证 GPU 上跑测试不抖动。

`get_detector_cfg` 是理解「测试配置从哪来」的钥匙，见 [mmdet3d/testing/model_utils.py:L59-L73](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/testing/model_utils.py#L59-L73)：它深拷贝 `config.model`，再把 `model.train_cfg`、`model.test_cfg` 包成独立 `Config` 后 **update 回 model 顶层**。这一步呼应 [u1-l5] 的辨析——正常训练时 Runner 会把 `model.train_cfg` 注入模型，脱离 Runner 的单测必须手动补齐同样的结构，否则 head 拿不到 assigner/nms 配置。`copy.deepcopy` 则保证多个测试改参数互不串扰。配置目录定位逻辑在 [mmdet3d/testing/model_utils.py:L23-L35](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/testing/model_utils.py#L23-L35)：以 `__file__` 上溯三级找到仓库根，再拼 `configs/`——所以测试**必须在源码仓库内运行**（或已 `pip install -e .`），裸 wheel 安装下找不到 configs 会直接抛异常。

`create_detector_inputs` 的签名即「模型输入契约的参数化」，见 [mmdet3d/testing/model_utils.py:L76-L95](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/testing/model_utils.py#L76-L95)：`bboxes_3d_type` 先断言只允许三个坐标系取值，再用字典映射到框子类——这正是 [u2-l1] 三大坐标系约定在测试层的体现。

组装 `gt_instances_3d` 的三行核心在 [mmdet3d/testing/model_utils.py:L124-L132](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/testing/model_utils.py#L124-L132)：随机张量直接包成对应坐标系 3D 框对象，`labels_3d` 随机整数，`Det3DDataSample(metainfo=dict(box_type_3d=...))` 把框类写进元信息——[u3-l1] 讲过 `box_type_3d` 是数据集构造参数，这里验证模型侧同样依赖它。最终返回值 [mmdet3d/testing/model_utils.py:L154](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/testing/model_utils.py#L154) 是 `dict(inputs=inputs_dict, data_samples=[data_sample])`，与 `Pack3DDetInputs` 产出、`data_preprocessor` 消费的形态完全同构。

数据侧的 `create_dummy_data_info` 是一条手写的完整 KITTI info 字典，见 [mmdet3d/testing/data_utils.py:L10-L171](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/testing/data_utils.py#L10-L171)：`images` 段含四相机的 `cam2img` 与 `R0_rect`（[u3-l5] 讲过的离线算好标定），`lidar_points` 段含 `lidar2cam`/`Tr_velo_to_cam`/`Tr_imu_to_velo`，`instances` 段含 2D/3D 双标注。而 [mmdet3d/testing/data_utils.py:L174-L182](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/testing/data_utils.py#L174-L182) 的 `create_data_info_after_loading` 展示了「用真实 transform 加工伪 info」的技巧：直接实例化 `LoadPointsFromFile` 和 `LoadAnnotations3D` 顺序执行——测 transforms 不需要跑整条 pipeline。

#### 4.2.4 代码实践

1. **实践目标**：亲手体验伪数据工厂的输出形态，把它与你 [u8-l3] 自定义模型的测试需求对上。
2. **操作步骤**（示例代码）：

   ```python
   from mmdet3d.testing import create_detector_inputs, setup_seed

   setup_seed(0)
   batch = create_detector_inputs(num_gt_instance=2, num_points=100)
   print(batch.keys())                    # dict_keys(['inputs', 'data_samples'])
   print(batch['inputs'].keys())          # ['points']
   ds = batch['data_samples'][0]
   print(ds.gt_instances_3d.bboxes_3d.tensor.shape)   # torch.Size([2, 7])
   print(ds.metainfo_keys)                # 含 box_type_3d / lidar2img 等
   ```

3. **需要观察的现象**：`inputs['points']` 是**长度为 batch_size 的列表**而非堆叠张量（点数不等，无法堆叠——呼应 [u4-l3]）；`bboxes_3d` 是 `LiDARInstance3DBoxes` 对象而非裸 tensor。
4. **预期结果**：打印形状与上面注释一致。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `get_detector_cfg` 要把 `train_cfg`/`test_cfg` 提升到 model 顶层，而 `get_model_cfg` 不做？

**答案**：`get_detector_cfg` 服务于完整检测器测试，检测器的 `loss()`/`predict()` 会读 `self.train_cfg`（assigner 配置）与 `self.test_cfg`（NMS 配置）构造任务模块；正常由 Runner 在 build 时注入，单测里靠这步 update 模拟。`get_model_cfg` 只取裸 backbone/neck 等组件配置，这些组件不消费 train/test cfg，无需提升。

**练习 2**：`create_detector_inputs(bboxes_3d_type='depth')` 时，返回的 `data_samples` 里哪两处会随之改变？

**答案**：一是 `gt_instances_3d.bboxes_3d` 的类型从 `LiDARInstance3DBoxes` 换成 `DepthInstance3DBoxes`（[model_utils.py:L91-L95](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/testing/model_utils.py#L91-L95) 的字典映射）；二是 metainfo 里的 `box_type_3d` 同步换成该类（L130），模型 head 解码预测框时按它实例化输出框。

### 4.3 三类测试范式精读

#### 4.3.1 概念说明

仓库里的测试文件按「输入来源」分三类，难度与保真度递增：

| 范式 | 输入来源 | 代表文件 | 测的是什么 |
| --- | --- | --- | --- |
| 裸函数式 | 真实迷你数据（tests/data） | `test_kitti_dataset.py` | 数据集解析的**正确性**（数值精确到小数） |
| 类式（TestCase） | 纯随机张量 | `test_det3d_data_sample.py` | 数据结构的**契约**（类型断言、异常路径） |
| 冒烟式 | 真实 config 建模型 + 工厂伪输入 | `test_voxelnet.py` | 模型组装与前后向的**连通性** |

三者互补：函数式抓回归（改了 parser 数值变了立刻红）、类式抓接口漂移（字段改名/类型变化）、冒烟式抓集成断裂（注册名改了、模块接口变了）。

#### 4.3.2 核心流程

冒烟测试（最高频的模型测试）的固定五步，见 4.2.2 流程图。它有两个防御性细节值得注意：

- `DefaultScope.get_instance('test_voxelnet', scope_name='mmdet3d')`：强制把当前默认 scope 设为 mmdet3d，防止同进程其他测试改过 scope 导致 `MODELS.build` 路由错乱（[u4-l1] 的 scope 机制在测试环境的对冲）。
- 用 `torch.cuda.is_available()` 包住整个测试体：无 GPU 的 CI（build_cpu 任务）会**静默跳过**而不是失败——同一个测试文件同时服务 CPU/GPU 两套 CI。

#### 4.3.3 源码精读

**范式一：裸函数式 + 真实数据。** [tests/test_datasets/test_kitti_dataset.py:L43-L87](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_datasets/test_kitti_dataset.py#L43-L87) 的 `test_getitem` 用 `tests/data/kitti` 真实构造 `KittiDataset`，然后逐层断言：路径拼接正确（L64-69，`lidar_path` 含 data_root 与 data_prefix）、`parse_ann_info` 产出字段类型正确（L74-87），其中最硬核的一行是 [tests/test_datasets/test_kitti_dataset.py:L82-L83](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_datasets/test_kitti_dataset.py#L82-L83)——**对整框张量求和后与 7.2650 做全等比较**。这个魔法数字是那一帧唯一目标的 7 个自由度之和，坐标转换链（相机系标注 → inv(lidar2cam) → 雷达系，[u3-l3]）任何一步数值漂移都会被这一个断言抓住。L89-108 则测试**子集类别训练**：`classes=['Car']` 后 Pedestrian/Cyclist 被 label_mapping 映掉，断言标注数归零。

这个文件还示范了「测试内注册临时 transform」的惯用法，见 [tests/test_datasets/test_kitti_dataset.py:L19-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_datasets/test_kitti_dataset.py#L19-L36)：`if 'Identity' not in TRANSFORMS` 守卫 + 类体内 `@TRANSFORMS.register_module()` 定义一个直通管线，手工把 `ann_info` 提升为 `data_samples`——绕开完整增强管线，让测试聚焦在数据集类本身。

**范式二：类式 + 随机张量。** [tests/test_structures/test_det3d_data_sample.py:L19-L31](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_structures/test_det3d_data_sample.py#L19-L31) 用 `unittest.TestCase` 组织，辅助函数 `_equal`（L12-16）统一比较 tensor/array/标量。`test_setter` 逐一塞入 `gt_instances_3d`、`pred_instances_3d`、`pts/img_pred_instances_3d`、`gt/pred_pts_seg` 六个字段并验证读回一致——正是 [u2-l4] 讲的六大数据契约的回归测试。最有价值的是异常路径 [tests/test_structures/test_det3d_data_sample.py:L112-L117](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_structures/test_det3d_data_sample.py#L112-L117)：`pytest.raises(AssertionError)` 断言「给 `pred_instances_3d` 塞裸 tensor 必须被 setter 的类型断言拦下」——测试不只验证功能，还验证**防线本身在工作**。

**范式三：冒烟式。** [tests/test_models/test_detectors/test_voxelnet.py:L14-L24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_detectors/test_voxelnet.py#L14-L24) 是三行式的标准开头：`DefaultScope` 定域 → `get_detector_cfg` 取 PointPillars 真实配置 → `MODELS.build` 建模（**用生产 config 建模**意味着配置文件本身的错误也会被抓到）。预测段 [tests/test_models/test_detectors/test_voxelnet.py:L41-L51](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_detectors/test_voxelnet.py#L41-L51) 断言输出含 `bboxes_3d/scores_3d/labels_3d` 三键；损失段 [tests/test_models/test_detectors/test_voxelnet.py:L68-L73](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_detectors/test_voxelnet.py#L68-L73) 只断言三项 loss **非负**——不校验数值大小（那依赖训练），只校验符号与键名存在。L26-39 被注释掉的 aug_test 代码块是诚实的 TODO：测试也能标记未支持的能力。

#### 4.3.4 代码实践

1. **实践目标**：跑通两个范式，体会 pytest 的选择性执行。
2. **操作步骤**：

   ```bash
   # 只跑一个文件
   pytest tests/test_datasets/test_kitti_dataset.py -v
   # 只跑一个类 / 一个方法（类式测试支持 :: 定位）
   pytest tests/test_structures/test_det3d_data_sample.py::TestDet3DDataSample::test_setter -v
   # 冒烟测试（无 GPU 时应观察 skip 行为；若依赖缺失先 pip install -r requirements/tests.txt）
   pytest tests/test_models/test_detectors/test_voxelnet.py -v
   ```

3. **需要观察的现象**：前两个是纯 CPU 测试，应稳定通过；`test_voxelnet` 在无 CUDA 环境下因整个方法体被 `if torch.cuda.is_available():` 包裹而**直接通过（0 条断言执行）**——这不是跳过标记（没有 `@unittest.skipIf`），是分支未进入。若本机装的是 CPU 版 mmcv，构建 PillarScatter 等算子时可能报错，属环境限制而非测试失败。
4. **预期结果**：`test_kitti_dataset` 与 `test_det3d_data_sample` 通过。待本地验证（依赖 mmcv/mmdet 完整安装）。

#### 4.3.5 小练习与答案

**练习 1**：`test_getitem` 里 `torch.allclose(ann_info['gt_bboxes_3d'].tensor.sum(), torch.tensor(7.2650))` 为什么用「求和后比较」而不是逐元素比较？

**答案**：求和把 7 个自由度压缩成一个标量，断言短且不依赖框内元素顺序；同时它对任何单元素漂移都敏感（误差会累积进和里）。代价是理论上存在误差互相抵消的漏检，但配单实例场景概率极低。这是「简洁」与「严格」的工程折中。

**练习 2**：为什么冒烟测试断言 `losses['loss_bbox'][0] >= 0` 而不断言其约等于某个值？

**答案**：loss 的具体数值取决于随机输入与初始化权重，固定种子后虽可复现，但换 torch 版本、换 CUDA 版本数值都会微变——写死数值会让测试在不同 CI 矩阵（torch 1.8 与 2.0）间随机红。断言非负是**环境无关**的不变量：L1/Focal 损失数学上恒非负，出现负值必是实现 bug。

**练习 3**：如果你为 [u8-l3] 的自定义 Anchor3DHead 子类写测试，该选哪个范式？

**答案**：冒烟式为主：`get_detector_cfg` 取挂了你 head 的 config（或手动改 `model.bbox_head.type`），`MODELS.build` 后用 `create_detector_inputs` 跑 loss 与 predict 两模式，断言 loss 键与非负。再补一个类式小测试验证你重写方法的独立逻辑（如自定义 decode 的输入输出形状），不依赖完整模型。

### 4.4 CI 流水线：路径过滤与测试矩阵

#### 4.4.1 概念说明

MMDetection3D 的 CI 用 CircleCI 的**动态配置**特性拆成两级：

- 第一级 [.circleci/config.yml]：`setup: true` 声明这是「预处理器」。`path-filtering` orb 用正则匹配本次 PR 改动的文件，输出一个布尔参数 `lint_only`——**只有当改动全部落在 mapping 之外时才保持 true（只查风格），但凡动了 `mmdet3d/`、`tests/`、`configs/`、`tools/`、`projects/`、`requirements/`、`.circleci/` 任一目录下的文件，就翻成 false（触发全量测试）**。
- 第二级 [.circleci/test.yml]：真正的工作流定义。`lint_only` 为真走 `pr_stage_lint`（只跑 lint），为假走 `pr_stage_test`（lint + CPU 矩阵 + 人工批准 + GPU 矩阵）。

这样设计的经济账：改一行 README 的 PR 不该烧半小时 GPU 机器时间；而 GPU 任务昂贵，入口处还有一道 `hold: type: approval` 的**人工审批闸门**——CPU 双版本全绿后，维护者点头才启动 GPU 测试。

#### 4.4.2 核心流程

```text
PR 提交
  ↓ config.yml: path-filtering 按正则扫描改动文件
改动触及代码目录? ──否──→ lint_only=true  → pr_stage_lint（仅 lint job）
  │是
  ↓ lint_only=false → pr_stage_test
lint（pre-commit 全量 + interrogate 文档覆盖率 ≥90%）
  ↓
build_cpu × minimum_version_cpu（torch 1.8.1 / python 3.7）
  ↓ 串行
build_cpu × maximum_version_cpu（torch 2.0.0 / python 3.9）
  ↓
hold（人工批准）
  ↓
build_cuda × mainstream_version_gpu（torch 1.8.1 / cuda 10.2）
build_cuda × maximum_version_gpu（torch 2.0.0 / cuda 11.7）
```

版本矩阵的设计意图：**最低版本卡死兼容下限**（python 3.7 + torch 1.8.1 是 `setup.py` 声明的地板），**最高版本预防未来脱节**。合入 `dev-1.x` 后另有 `merge_stage_test` 直接跑 GPU 全量。

#### 4.4.3 源码精读

第一级路径过滤的 mapping 表在 [.circleci/config.yml:L22-L30](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/.circleci/config.yml#L22-L30)：七行「正则 → 参数 → 值」三元组，任何一行命中即把 `lint_only` 置 false；对照基线是 `dev-1.x` 分支（L30）。`config-path` 指回 `test.yml`（L35）完成接力。

lint 任务的灵魂是两条命令，见 [.circleci/test.yml:L23-L28](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/.circleci/test.yml#L23-L28)：`pre-commit run --all-files` 全仓风格检查；`interrogate --fail-under 90 mmdet3d` 强制**文档字符串覆盖率不低于 90%**——这是很多项目没有的硬约束，解释了 mmdet3d 源码里几乎每个公有函数都有 docstring。

CPU 任务安装依赖的顺序在 [.circleci/test.yml:L57-L66](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/.circleci/test.yml#L57-L66)：mmengine（git main）→ mim 装 mmcv ≥ 2.0.0rc4 → mmdetection（dev-3.x）→ tests 依赖 → `pip install -e .` 本体。这正是 [u1-l2] 讲的依赖链顺序的 CI 版；跑测试的命令在 [.circleci/test.yml:L76-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/.circleci/test.yml#L76-L80)：`coverage run --branch --source mmdet3d -m pytest tests/` 全量测试并统计分支覆盖率。

工作流编排与人工闸门在 [.circleci/test.yml:L143-L186](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/.circleci/test.yml#L143-L186)：`pr_stage_test` 里 lint → minimum_version_cpu → maximum_version_cpu 串行（`requires` 链），`hold` 类型为 `approval`，两个 GPU 任务都 `requires: hold`。给你的直接启示：**PR 让 CPU 测试先绿，是节省维护者时间的第一礼貌**。

#### 4.4.4 代码实践

1. **实践目标**：学会在本地复现 CI 将对你 PR 执行的检查，避免「推上去才发现红」。
2. **操作步骤**：

   ```bash
   pip install -U pre-commit interrogate
   pre-commit run --all-files          # 复现 lint job 的第一条命令
   interrogate -v --ignore-init-method --ignore-module \
     --ignore-nested-functions --ignore-magic \
     --ignore-regex "__repr__" --fail-under 90 mmdet3d   # 复现第二条
   pytest tests/test_structures/ -q    # 复现 unittests（先跑最快的子集）
   ```

3. **需要观察的现象**：`pre-commit` 首次运行会下载各钩子环境，随后逐文件报风格问题并可能**自动改写文件**（yapf/isort 会直接修）；`interrogate` 会列出缺 docstring 的函数清单及总覆盖率百分比。
4. **预期结果**：在未做任何改动的干净仓库上，两项检查应基本通过（官方仓库自身是绿的）；pre-commit 若自动改写了文件，需重新 `git add`。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：你提交了一个只改 `docs/en/` 下两个 md 文件的 PR，CI 会跑 GPU 测试吗？

**答案**：不会。mapping 七条正则都不匹配 `docs/.*`，`lint_only` 保持默认 true，只触发 `pr_stage_lint` 的 lint job（含 mdformat 检查）。这就是路径过滤的省时逻辑。

**练习 2**：为什么 `minimum_version_cpu` 与 `maximum_version_cpu` 之间用 `requires` 串行，而不是并行？

**答案**：双重考虑。其一，失败定位快：若两个并行都红，分不清是共性 bug 还是特定版本问题；串行时最低版本先红就提前止损，不再烧高版本机器时间。其二，资源礼仪：CircleCI 按机器时间计费，串行让明显错误的 PR 只消耗一半资源。这是 CI 编排里常见的「快速失败（fail fast）」模式。

### 4.5 贡献流程与代码规范

#### 4.5.1 概念说明

官方贡献指南（`docs/en/notes/contribution_guides.md`）把一次 PR 的生命周期规定为六步：**取最新代码 → 从 dev-1.x 切分支 → 提交（强制 pre-commit）→ 推送并开 PR → 按评审意见修改 → 合并后删分支**。对二次开发者，即使不打算给官方提交，也强烈建议照此流程管理自己的 fork——它同时解决了「基线选择」「风格统一」「改动原子性」三个协作痛点。

两条最重要的分支事实：

- PR 基线必须是 **`dev-1.x`** 而非 `master`：`dev-1.x` 依赖 MMEngine 且是主维护分支（本手册全部分析都基于它）。
- GitHub 默认 PR 基线是 master，**开 PR 后必须手动把 base branch 改成 dev-1.x**——这是新人贡献者最常踩的坑。

#### 4.5.2 核心流程

```text
fork 官方仓库 → clone 自己的 fork → git remote add upstream 官方地址
  ↓
git checkout dev-1.x && git pull upstream dev-1.x   # 永远在最新基线上开工
  ↓
git checkout -b my-feature                            # 一个分支只对一件事
  ↓
编码 + 为新功能写测试（放进 tests/ 对应镜像目录）
  ↓
git add … && git commit -m '…'    # pre-commit 钩子自动跑风格检查，可能改写文件→重新 add 再提交
  ↓
git push origin my-feature → GitHub 开 PR → 【手动把 base 改成 dev-1.x】
  ↓
按评审意见迭代 → 维护者合并 → 删除特性分支
```

PR 标题规范：`[Prefix] Short description (Suffix)`，前缀取 `[Feature]`（新功能）、`[Fix]`（修 bug）、`[Docs]`（文档）、`[WIP]`（开发中，暂缓评审）之一。

#### 4.5.3 源码精读

六步流程的完整定义在 [docs/en/notes/contribution_guides.md:L27-L118](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/notes/contribution_guides.md#L27-L118)。其中 pre-commit 的初始化命令在 [docs/en/notes/contribution_guides.md:L69-L74](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/notes/contribution_guides.md#L69-L74)：`pip install -U pre-commit && pre-commit install`——后者把钩子注册进本仓库 `.git/hooks/`，此后每次 `git commit` 自动触发。

「改 base 分支」的特别提醒见 [docs/en/notes/contribution_guides.md:L99-L101](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/notes/contribution_guides.md#L99-L101)。

PR 原子性规范（PR Specs）在 [docs/en/notes/contribution_guides.md:L120-L140](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/notes/contribution_guides.md#L120-L140)，官方给的粒度示例很传神：Bad 是「Support Faster R-CNN」（整模型太大），Good 是「给 box head 加一个参数以支持自定义卷积层数」——**一个 PR 只做一件可在一屏内看完的事**。

钩子清单定义在 [.pre-commit-config.yaml:L1-L59](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/.pre-commit-config.yaml#L1-L59)：flake8（语法/未用变量）、isort + yapf（导入排序与格式化，参数在 `setup.cfg`）、codespell（拼写）、mdformat（Markdown）、docformatter（docstring 包装），以及 OpenMMLab 自家的 `check-algo-readme` 与 `check-copyright`（每个源文件必须带 `# Copyright (c) OpenMMLab. All rights reserved.` 头——这也是你在所有测试文件第一行看到它的原因）。

版本史沉淀在 [docs/en/notes/changelog.md:L3-L30](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/notes/changelog.md#L3-L30)：每个版本按 Highlights / New Features / Improvements / Bug Fixes / Contributors 分节，条目全部挂 PR 编号。写 PR 时在描述里把动机、改动、影响写清楚，就是在为这份日志供稿——它是「文档漂移」的解药，也是你升级版本时判断 breaking change 的第一手资料（呼应 [u7-l5] 「工具用法以脚本 --help 为准」的批判性阅读习惯）。

#### 4.5.4 代码实践

1. **实践目标**：为自己的 fork 建立规范的贡献工作流，并演练一次「不推远端」的完整提交。
2. **操作步骤**：
   - 在你的 fork 中执行 `pip install -U pre-commit && pre-commit install`；
   - 从 `dev-1.x` 切出分支 `docs/my-note`，随便找 [u8-l3] 你写过的自定义模块文件（或在 tests/ 下新建一个测试文件，记得首行加版权头）；
   - 故意写一行超长代码与一个未使用变量，`git commit` 观察钩子拦截；修复后再次提交成功；
   - `git branch -d docs/my-note` 清理。
3. **需要观察的现象**：commit 时 flake8 报 `F401 imported but unused`、yapf 自动重排格式；不修复则提交被钩子中断（退出码非 0）。
4. **预期结果**：修复后提交成功，`git log -1 --stat` 只含你预期的文件。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么指南强烈建议先 `git checkout dev-1.x && git pull upstream dev-1.x` 再切分支，而不是直接从本地旧分支切？

**答案**：若基线落后，你的分支会包含「官方已演进的部分」与「你的改动」的混合，开 PR 后 diff 里会出现大量与你无关的冲突改动，评审无法阅读；且合并冲突概率大。从最新基线切分支保证 PR 的 diff **纯净地等于你的改动**。

**练习 2**：`check-copyright` 钩子与 CI lint 里的 `interrogate --fail-under 90` 分别在管什么？如果你新增一个源文件漏了版权头，会在哪个环节被抓？

**答案**：`check-copyright` 管每个文件的版权声明头是否存在；`interrogate` 管 docstring 覆盖率是否 ≥90%。漏版权头会在**本地 commit 时**（pre-commit）先被拦，即便绕过（如 `--no-verify`），CI 的 `pre-commit run --all-files` 也会红——本地与 CI 双保险。

## 5. 综合实践

**任务：为你 [u8-l3] 的自定义模块（或任选一个 mmdet3d 模块）写一套完整的测试，并走一遍「本地 CI 预演」。**

以「自定义 Anchor3DHead 子类 `MyAnchor3DHead`」为例（换成你的任何模块皆可）：

1. **选目录与文件名**：按镜像原则，head 属于 `mmdet3d/models/dense_heads/`，测试放 `tests/test_models/test_dense_heads/test_my_anchor3d_head.py`；文件首行写版权头。
2. **选范式**：主测试用冒烟式；若你的 head 有独立的 decode/loss 逻辑，再补一个类式测试单独打它。
3. **搭骨架**（示例代码）：

   ```python
   # Copyright (c) OpenMMLab. All rights reserved.
   import unittest

   import torch
   from mmengine import DefaultScope

   from mmdet3d.registry import MODELS
   from mmdet3d.testing import create_detector_inputs, get_detector_cfg, setup_seed


   class TestMyAnchor3DHead(unittest.TestCase):

       def test_my_anchor3d_head(self):
           import mmdet3d.models  # noqa: F401 触发注册（呼应 u4-l1 懒导入）
           DefaultScope.get_instance('test_my_head', scope_name='mmdet3d')
           setup_seed(0)
           # 取挂了你 head 的 config；或取 PointPillars 配置后改指向：
           cfg = get_detector_cfg(
               'pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py')
           cfg.bbox_head.type = 'MyAnchor3DHead'          # 替换为你注册的类型名
           model = MODELS.build(cfg)

           packed_inputs = create_detector_inputs(num_gt_instance=2)
           if torch.cuda.is_available():
               model = model.cuda()
               data = model.data_preprocessor(packed_inputs, True)
               with torch.no_grad():
                   results = model.forward(**data, mode='predict')
               self.assertIn('bboxes_3d', results[0].pred_instances_3d)
               losses = model.forward(**data, mode='loss')
               self.assertGreaterEqual(losses['loss_cls'][0], 0)
   ```

4. **跑通并核对三个观察点**：`DefaultScope` 是否与 `default_scope='mmdet3d'` 一致；`create_detector_inputs` 的 `bboxes_3d_type` 是否与 config 匹配（KITTI 是 lidar）；无 GPU 环境下测试是否「零断言通过」而非报错。
5. **本地 CI 预演**：`pre-commit run --files tests/test_models/test_dense_heads/test_my_anchor3d_head.py`，再 `pytest` 跑它，最后用 `interrogate` 检查你新加的 docstring 是否达标。
6. **（可选）走完整流程**：按 4.5 的六步把测试与模块一起提交到你 fork 的特性分支。

预期产物：一个绿色通过的测试文件 + 一段「它验证了哪三件事」的自我说明。若无法本地运行，标注「待本地验证」并说明卡在哪个依赖。

## 6. 本讲小结

- `tests/` 采用**镜像原则**组织：`mmdet3d/<包>/<模块>` 的测试就在 `tests/test_<包>/test_<模块>`，找不到时按类别目录（如 `test_detectors`）降级。
- `tests/data/` 内置单帧级迷你数据集（KITTI 一帧 + info pkl + GT 数据库），让数据集测试无需下载、路径真实、数值可精确断言（如 7.2650 魔法数）。
- `mmdet3d/testing` 是**随包发布的伪数据工厂**：`data_utils` 造假 info 字典，`model_utils` 的 `create_detector_inputs` 直接产出 `dict(inputs, data_samples)` 契约、`get_detector_cfg` 从真实 config 建模并补齐 train/test cfg、`setup_seed` 固定全部随机源。
- 三类测试范式各有分工：裸函数式抓数值回归、类式抓数据契约与异常防线、冒烟式抓「config 建模 + 前后向」连通性；冒烟测试只断言环境无关的不变量（loss 非负、字段存在）。
- CI 是两级结构：`config.yml` 用路径过滤把纯文档 PR 降级为 lint-only；`test.yml` 编排 lint（含 docstring 覆盖率 ≥90%）→ CPU 双版本串行 → 人工批准 → GPU 双版本的矩阵。
- 贡献六步流程的三个关键动作：基线选 `dev-1.x`（开 PR 后手动改 base）、commit 前 `pre-commit install`、一个 PR 只做一件原子的事。

## 7. 下一步学习建议

至此，8 个单元 43 篇讲义全部完成。接下来建议：

1. **动手闭环**：把 [u8-l2]（自定义数据集）→ [u8-l3]（自定义模型）→ 本讲的测试框架串成一个完整小项目：自有数据 + 自有 head + 绿色测试 + 符合规范的分支。
2. **通读一个 test_*.py 全家**：`tests/test_datasets/test_nuscenes_dataset.py` 是最厚的数据集测试，能看到 sweeps、多相机、评估全链路的断言写法，可与 [u3-l3] 对照阅读。
3. **研读 mmengine 的测试基建**：`mmdet3d/testing` 的设计（工厂 + 种子 + config 定位）源自 MMEngine 与 MMDetection 的同名模块，对比三者的 `testing/utils.py` 能看清 OpenMMLab 测试范式的演化。
4. **提一个真实 PR**：从 `[Docs]` 修一处文档错漏或 `[Fix]` 一个小 bug 开始（可从 changelog 的 Bug Fixes 节找线索），完整走一遍评审流程——这是对本手册所有内容的最终检验。
