# pyngp 绑定架构

## 1. 本讲目标

本讲讲解 instant-ngp 的 Python 绑定模块 **pyngp** 是如何被造出来的。

读完本讲，你应该能够：

1. 说出 `pyngp` 模块由哪个源文件、用哪个库、在哪一个编译开关下生成。
2. 看懂 `src/python_api.cu` 里 `PYBIND11_MODULE(pyngp, m)` 这一个巨型代码块如何把 C++ 的「上帝对象」`Testbed`（见 u2-l1）整体搬到 Python 里。
3. 区分 `def` / `def_readwrite` / `def_readonly` / `def_property` 四种绑定写法，并能解释为什么有的成员用直接映射、有的用 lambda。
4. 理解 numpy 数组（`render_to_cpu`、网格导出、`set_image`）与 JSON 网络配置在 C++ 和 Python 之间是如何互转的。
5. 写出一段最小的 pyngp 脚本：构造 `Testbed` → 加载 fox → 训练若干步 → 保存快照。

## 2. 前置知识

- **pybind11 是什么**：一个纯头文件的 C++ 库，让你在 C++ 里写「胶水代码」，把 C++ 的类、函数、枚举注册成 Python 可见的对象。`import pyngp` 时，Python 解释器会调用编译进 `.so` 的模块初始化函数，这个函数就是 pybind11 生成的。instant-ngp 通过 `dependencies/pybind11` 子模块引入它（见 u1-l2、u1-l3）。
- **Testbed 是「上帝对象」**：几乎所有状态（GPU、模式、网络、相机、训练临时态）都挂在 `Testbed` 这一个类上（u2-l1）。所以只要把 `Testbed` 暴露给 Python，就等于把整个项目的能力暴露给 Python。README 明确写道：「GUI 的全部功能（甚至更多）都有 Python 绑定」。
- **pyngp 是第二条构建产物**：u1-l3 讲过，CMake 把核心源码先编成静态库 `ngp`，再链接出可执行文件 `instant-ngp` 与 Python 共享库 `pyngp`。后者由 `NGP_BUILD_WITH_PYTHON_BINDINGS`（对应宏 `NGP_PYTHON`）开关控制；没有这个开关，本讲引用的所有 `#ifdef NGP_PYTHON` 代码都不会编译。
- **numpy 与 nlohmann::json**：pyngp 用 numpy 数组搬运图像/网格等大块数值数据，用 JSON 表示网络配置。前者靠 `<pybind11/numpy.h>`，后者靠 `<pybind11_json/pybind11_json.hpp>`，它让 C++ 的 `nlohmann::json` 与 Python 的 `dict` 自动互转。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/python_api.cu` | 唯一的绑定源文件。文件上半部分实现了若干「桥接函数」（把 C++ 内部结果拷成 numpy 数组等），下半部分是 `PYBIND11_MODULE(pyngp, m)` 巨型块，逐项注册枚举、辅助类、`Testbed` 及其嵌套类型。 |
| `include/neural-graphics-primitives/testbed.h` | `Testbed` 类声明。pyngp 绑定的每一个方法/成员都能在这里找到对应的 C++ 声明（部分受 `#ifdef NGP_PYTHON` 保护）。 |
| `include/neural-graphics-primitives/pybind11_vec.hpp` | 本仓库自带的「向量/矩阵类型转换器」参考实现，展示 `ngp::tvec<T,N>` / `ngp::tmat<T,N,M>` 如何与 numpy 数组互转（含列主序↔行主序的转置）。它清晰说明了 type_caster 机制；线上构建里同款机制由 `python_api.cu` 第 26 行包含的 `<tiny-cuda-nn/vec_pybind11.h>` 提供——因为 `common.h` 里有 `using namespace tcnn;`，`ngp::vec3` 与 `tcnn::vec3` 本就是同一个类型。 |

> 说明：`pybind11_vec.hpp` 引用的 `<neural-graphics-primitives/vec.h>` 在当前代码树里已不存在，因此该文件当前并未被任何源文件直接包含，可视作「讲解 type_caster 机制的参考文档」。运行时真正生效的转换器是 tiny-cuda-nn 的同名文件，二者结构一致，本讲据此讲解原理。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**pybind11 模块注册**、**Testbed Python API**、**numpy/json 互转**。

---

### 4.1 pybind11 模块注册

#### 4.1.1 概念说明

`import pyngp` 这一行背后发生的事：Python 加载编译好的 `pyngp.so`（或 `.pyd`），找到其模块初始化入口并调用。pybind11 用一个宏把这个入口伪装成一段普通的 C++ 代码块，这段块里你逐条「注册」要暴露给 Python 的东西。instant-ngp 把全部注册工作集中写在 `src/python_api.cu` 的一个函数里：

- 注册枚举（如 `TestbedMode`、`RenderMode`、`LossType`）—— Python 里就成了 `ngp.TestbedMode.Nerf`。
- 注册自由函数（如 `mode_from_scene`）—— 成为模块级函数 `ngp.mode_from_scene(...)`。
- 注册辅助类（如 `BoundingBox`、`Lens`、`fs::path`）—— 成为 `ngp.BoundingBox` 等。
- 注册主类 `Testbed` 及其嵌套类（`Nerf`、`Sdf`、`Image`、`CameraPath` ……）。

#### 4.1.2 核心流程

模块注册的核心流程可以概括为：

```text
import pyngp
   │
   ▼  Python 调用模块初始化函数
PYBIND11_MODULE(pyngp, m) {      ← m 是 py::module_ 句柄
   m.doc() = "...";               ← 模块文档字符串
   m.def("自由函数", ...);        ← 模块级函数
   py::enum_<EXxxEnum>(m, "Xxx")  ← 枚举
      .value("A", EXxxEnum::A)
      .export_values();
   py::class_<Xxx>(m, "Xxx")      ← 类
      .def(方法)
      .def_readwrite(成员);
}
```

`.export_values()` 把枚举值导出到模块作用域（既能写 `ngp.TestbedMode.Nerf`，也能在某些场合直接用 `ngp.Nerf`）；`.value()` 里第一个字符串是 Python 侧的名字，第二个是 C++ 枚举值。

#### 4.1.3 源码精读

模块入口与文档字符串：[src/python_api.cu:306-307](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L306-L307) 注册了模块名 `pyngp` 并设置 docstring。紧接着第 309 行 `m.def("free_temporary_memory", ...)` 把一个释放 GPU 显存池的自由函数暴露为 `ngp.free_temporary_memory()`。

枚举注册示例——`TestbedMode`：[src/python_api.cu:317-323](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L317-L323) 把 u2-l1 讲过的 `ETestbedMode`（Nerf/Sdf/Image/Volume/None）导出为 `ngp.TestbedMode`。与之并列的还有 `TrainMode`（311-315）、`RenderMode`（333-342）、`LensMode`（391-399）等十余个枚举。

值得注意的是带「历史别名」的枚举：[src/python_api.cu:351-362](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L351-L362) 注册 `LossType` 时，第 357-359 行把旧的 `SmoothL1` 也指向 `Huber`，这样旧脚本里写 `ngp.LossType.SmoothL1` 仍能运行。同理 `TrainingImageMetadata` 把旧名 `camera_distortion` 别名到新成员 `lens`（757-758 行）。

辅助类的注册——`BoundingBox`：[src/python_api.cu:409-427](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L409-L427) 用 `py::class_<BoundingBox>(m, "BoundingBox")` 注册，链式 `.def(...)` 暴露方法，最后 `.def_readwrite("min"/"max", ...)` 直接暴露两个 `vec3` 成员。注意 `enlarge` 有两个重载，用 `py::overload_cast<const vec3&>(...)` 显式挑出某一个重载（417-418 行），这是 pybind11 处理重载函数的标准写法。

一个让 Python 更好用的关键技巧——隐式转换：[src/python_api.cu:435-437](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L435-L437) 注册了 `fs::path` 类，并声明 `py::implicitly_convertible<std::string, fs::path>()`。这意味着：凡 C++ 接口要求 `fs::path` 的地方，Python 侧都可以直接传字符串，例如 `testbed.load_training_data("data/nerf/fox")`。如果没有这行，每个路径都得先包成 `ngp.path("...")`。

#### 4.1.4 代码实践

**实践目标**：确认 `pyngp` 在你环境里可用，并观察模块注册的产物。

**操作步骤**（前提：已按 u1-l3 编出 `pyngp`，`pip install -r requirements.txt` 已装好依赖）：

1. 在仓库根目录启动 Python，确保能找到模块（若 `pyngp` 装在别处，需把其目录加入 `PYTHONPATH`）。
2. 运行下面这段示例代码（**示例代码**，非项目原有）：

   ```python
   import pyngp as ngp

   print(ngp.__doc__)                       # 模块 docstring（来自 m.doc()）
   print(ngp.TestbedMode.Nerf)              # 枚举成员（来自 .value("Nerf", ...)）
   print(ngp.mode_from_scene("data/nerf/fox"))   # 自由函数 → 预期 TestbedMode.Nerf
   print([x for x in dir(ngp.Testbed) if not x.startswith("_")][:10])  # 绑定的方法/成员
   ```

**需要观察的现象**：`__doc__` 打印出 `Instant neural graphics primitives`；`mode_from_scene` 对目录返回 `TestbedMode.Nerf`；`dir(ngp.Testbed)` 列出 `load_training_data`、`render`、`train`、`save_snapshot` 等名字。

**预期结果**：你能通过 Python 访问到所有在 `PYBIND11_MODULE` 里注册过的枚举、函数和类。

**待本地验证**：若报 `No module named 'pyngp'`，说明编译时未开启 Python 绑定或模块不在搜索路径，回到 u1-l3 检查 `NGP_BUILD_WITH_PYTHON_BINDINGS` 与 CMake 日志。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ngp.LossType.SmoothL1` 和 `ngp.LossType.Huber` 指向同一个值？
**答案**：因为在 [src/python_api.cu:357-359](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L357-L359) 中，`SmoothL1` 是旧名，被显式 `.value("SmoothL1", ELossType::Huber)` 别名到 `Huber`，用于向后兼容旧脚本。

**练习 2**：如果删掉 `py::implicitly_convertible<std::string, fs::path>()`，下面哪行会出错？`testbed.load_file("fox.ingp")`、`testbed.load_file(ngp.path("fox.ingp"))`。
**答案**：前者会出错，因为 `load_file` 形参是 `const fs::path&`，没有隐式转换后 Python 字符串不再被接受；后者显式构造了 `ngp.path` 仍可用。

---

### 4.2 Testbed Python API

#### 4.2.1 概念说明

`Testbed` 这个上帝对象承载了项目的全部能力（u2-l1）。pyngp 用 `py::class_<Testbed>(m, "Testbed")` 把它整类暴露，于是你在 Python 里拿到一个 `ngp.Testbed` 对象后，等于拿到了一个没有 GUI 外壳、但功能更全的 instant-ngp。理解这块绑定的关键，是分清四种「暴露成员」的写法：

| 写法 | 含义 | 典型用途 |
| --- | --- | --- |
| `.def("name", &Testbed::method)` | 暴露成员函数 | `load_training_data`、`train`、`render` |
| `.def_readwrite("name", &Testbed::member)` | 直接读写公开成员变量 | `shall_train`、`render_mode`、`camera_matrix` |
| `.def_readonly("name", &Testbed::member)` | 只读公开成员 | `mode`、`training_step`、`loss` 相关只读量 |
| `.def_property("name", getter, setter)` | 经函数/lambda 读写的「属性」 | `scale`、`dlss`、`root_dir`（需转换或带校验） |

选择哪种写法的判断标准：能直接当变量访问的用 `def_readwrite`；逻辑上只读的用 `def_readonly`；读取/赋值需要做类型转换、计算或副作用校验的，用 `def_property` 配 lambda。

#### 4.2.2 核心流程

一个典型的 pyngp 控制流（也是 `scripts/run.py` 的骨架）：

```text
ngp.Testbed(...)                 ← 构造（默认 mode=None）
   │
   ├─ load_training_data(path)   ← 加载数据，mode 由 path 自动判定
   ├─ shall_train = True         ← 显式开启训练（见 4.2.3 的坑）
   │
   while training_step < N:
   │   testbed.frame()           ← 一帧 = 训练一步 + 渲染（等价于 GUI 主循环）
   │   # 或 testbed.train(batch_size) 只训练不渲染
   │
   ├─ render(width, height, spp) ← 无窗渲染出 numpy 图
   └─ save_snapshot(path, False) ← 保存 .ingp 快照
```

`frame()` 与 GUI 程序里 u2-l2 讲的 `frame()` 是同一个函数；`train()` 只做训练准备+训练一步，不渲染；`render()` 走无头渲染路径，不需要窗口。

#### 4.2.3 源码精读

**类与构造函数**：[src/python_api.cu:439-443](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L439-L443) 注册了三个构造函数重载：无参（`mode=None`）、`(mode, data_path, network_path)`、`(mode, data_path, json_config)`，以及只读成员 `mode`（即 `m_testbed_mode`，对应 u2-l1 的模式枚举）。

**加载与训练的核心方法**：

- `load_training_data`：[src/python_api.cu:452](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L452) 绑定，注意带了 `py::call_guard<py::gil_scoped_release>()`。
- `frame`：[src/python_api.cu:504-506](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L504-L506) 绑定到 u2-l2 的 `Testbed::frame()`，同样释放 GIL。
- `train`：[src/python_api.cu:533](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L533) 绑定到 `void train(uint32_t batch_size)`（声明见 [testbed.h:500](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L500)）——注意它**只训练一步**，批量大小由参数给定。

> **关于 `py::call_guard<py::gil_scoped_release>()`**：`load_training_data`、`frame`、`train` 都是耗时 GPU 操作。默认情况下 pybind11 在调用 C++ 期间持有 Python 全局解释器锁（GIL），这会阻塞其他 Python 线程。`call_guard<gil_scoped_release>()` 告诉 pybind11「进入 C++ 后立即释放 GIL，返回前再重新获取」，这样长任务期间别的 Python 线程能继续跑。凡是可能跑得久的绑定都该加它。

> **一个容易踩的坑**：`m_train`（Python 名 `shall_train`）在 testbed.h 里默认是 `false`（[testbed.h:631](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L631)）。只有经 `load_file` 首次拖入数据时才会被自动置 true（[testbed.cu:407](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L407)）；而直接调用 `load_training_data` **不会**自动开启训练。因此 `scripts/run.py` 第 143 行显式写了 `testbed.shall_train = True`。你在自己脚本里也要手动开。

**渲染与快照**：

- `render`：[src/python_api.cu:507-519](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L507-L519) 绑定到 `render_to_cpu_rgba`（声明 [testbed.h:522-523](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L522-L523)），返回一张 `(H,W,4)` 的 numpy RGBA 图。`render_with_depth`（520-532）额外返回深度图。
- `save_snapshot`：[src/python_api.cu:563-570](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L563-L570) 三个参数 `path / include_optimizer_state / compress`，对应 u6-l3 讲的 `.ingp`/`.msgpack` 快照；`load_snapshot`（571 行）用 `py::overload_cast<const fs::path&>` 选定「按路径加载」的重载。
- `load_file`：[src/python_api.cu:573-578](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L573-L578) 是 u2-l3 讲的统一入口，自动判别快照/数据/配置/相机路径。
- `compute_and_save_marching_cubes_mesh`：[src/python_api.cu:592-604](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L592-L604) 对应 u6-l4 的网格导出，支持 OBJ/PLY；`compute_marching_cubes_mesh`（605-615）则把网格作为 numpy 数组直接返回（见 4.3）。

**四种「暴露成员」写法的实例对照**：

- `def_readwrite` 直接映射：[src/python_api.cu:622](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L622) `shall_train → m_train`、[第 628 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L628) `render_mode → m_render_mode`、[第 660 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L660) `camera_matrix → m_camera`。Python 侧 `testbed.render_mode = ngp.RenderMode.Shade` 直接改 C++ 成员。
- `def_readonly` 只读映射：[src/python_api.cu:670-673](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L670-L673) `training_step`、`nerf`、`sdf`、`image` 都只读——训练步数不该被外部随意改写，模式专属结构体也只暴露读取。
- `def_property_readonly` + lambda（读出来的值需要「算」一下）：[src/python_api.cu:669](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L669) `loss` 并非直接成员，而是从 `m_loss_scalar.val()` 取值，所以用一个 lambda `[](py::object& obj){ return obj.cast<Testbed&>().m_loss_scalar.val(); }`。
- `def_property` 带校验的 setter：[src/python_api.cu:687-701](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L687-L701) `dlss` 的赋值 lambda 会先检查 `m_dlss_provider` 是否就绪、窗口是否已建，否则抛 `runtime_error`——这就是它不能简单用 `def_readwrite` 的原因。
- `def_property` 做类型转换：[src/python_api.cu:706-710](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L706-L710) `root_dir` 在 C++ 侧是 `fs::path`，但 Python 侧希望是 `str`，于是 getter 返回 `root_dir().str()`、setter 接收字符串再调 `set_root_dir`。

**嵌套类**：[src/python_api.cu:714-715](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L714-L715) 用 `py::class_<Testbed::Nerf>(testbed, "Nerf")` 把 `Nerf` 注册为 `Testbed` 的内嵌作用域，于是 Python 里访问路径是 `testbed.nerf`（来自 671 行的 `def_readonly`）→ `testbed.nerf.training`（715 行）→ `testbed.nerf.training.optimize_extrinsics`（[788 行起](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L788)）。`Sdf`（855）、`Image`（873）、`NerfDataset`（766）、`CameraPath`（929）等同理。

#### 4.2.4 代码实践

**实践目标**：在 `PYBIND11_MODULE` 里精确定位四个关键方法的绑定行。

**操作步骤**：打开 `src/python_api.cu`，搜索下列 Python 方法名，记录其所在行号与绑定的 C++ 函数：

| Python 方法 | 绑定关键字 | 绑定到的 C++ 函数 |
| --- | --- | --- |
| `render` | `.def("render",` | `Testbed::render_to_cpu_rgba` |
| `load_training_data` | `.def("load_training_data",` | `Testbed::load_training_data` |
| `save_snapshot` | `.def("save_snapshot",` | `Testbed::save_snapshot` |
| `compute_and_save_marching_cubes_mesh` | `.def("compute_and_save_marching_cubes_mesh",` | `Testbed::compute_and_save_marching_cubes_mesh` |

**需要观察的现象**：四个方法都在 `py::class_<Testbed> testbed(...)` 的链式调用里（439-711 行之间），且 `render`、`save_snapshot` 都带 `py::arg(...)` 默认值。

**预期结果**：`render` ≈ 507 行、`load_training_data` ≈ 452 行、`save_snapshot` ≈ 563 行、`compute_and_save_marching_cubes_mesh` ≈ 592 行（以本仓库 HEAD 为准）。

#### 4.2.5 小练习与答案

**练习 1**：`testbed.loss` 为什么用 `def_property_readonly` + lambda，而不是 `def_readonly`？
**答案**：因为 `loss` 不是一个直接成员变量，需要调用 `m_loss_scalar.val()` 才能拿到当前值（[src/python_api.cu:669](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L669)）。`def_readonly` 只能映射到成员变量，无法接函数调用，所以用 lambda。

**练习 2**：把 `dlss` 的绑定从 `def_property` 改成 `def_readwrite` 会有什么后果？
**答案**：会丢失赋值时的校验逻辑（[src/python_api.cu:691-696](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L691-L696)）。用户在没初始化窗口时 `testbed.dlss = True` 将不再抛错，而是静默赋一个无效值，后续渲染才在别处崩溃，错误更难定位。

**练习 3**：`train(batch_size)` 调用一次，训练了多少步？
**答案**：一步。声明 `void train(uint32_t batch_size)`（[testbed.h:500](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L500)）表示「以给定批量大小执行一次训练」，参数是批量大小不是步数。要多步训练需循环，或用 `frame()`。

---

### 4.3 numpy/json 互转

#### 4.3.1 概念说明

pyngp 与 Python 之间有两类「大数据」要搬运：

1. **数值数组（图像、深度、网格顶点）**：用 numpy。pybind11 提供 `py::array_t<T>` 表示一个类型确定的 numpy 数组。C++ → Python 时构造一个 `py::array_t<float>` 返回；Python → C++ 时用 `array.request()` 拿到 `buffer_info`（含 `ndim`、`shape`、`ptr`），直接读写底层内存。
2. **配置对象（网络 JSON）**：用 nlohmann::json。`<pybind11_json/pybind11_json.hpp>` 给 `nlohmann::json` 注册了 type_caster，于是 Python `dict`/`list`/`int`/`float`/`str` 与 C++ `json` 自动互转，无需手写胶水。
3. **小向量/矩阵（vec3、mat4x3 等）**：靠 type_caster 模板把 `ngp::tvec` / `ngp::tmat` 与一维/二维 numpy 数组对接。

理解这三套机制后，你会明白为什么 `testbed.render(...)` 直接返回 numpy 图、`testbed.reload_network_from_json({...})` 能吃一个 dict。

#### 4.3.2 核心流程

**C++ → numpy（返回渲染图）**：

```text
1. 在 GPU 上渲染到 CUDA surface；
2. 构造 py::array_t<float> result({H, W, 4})；   ← 形状
3. cudaMemcpy2DFromArray(result.ptr, ..., DeviceToHost);
4. return result;   ← pybind11 自动把 array_t 包成 numpy.ndarray
```

**Python → C++（接收图像）**：

```text
1. 形参声明为 py::array_t<float> img；
2. py::buffer_info buf = img.request();   ← 拿到形状/指针
3. 校验 buf.ndim、buf.shape；
4. 用 buf.ptr 读取像素数据，拷进显存。
```

**json**：直接把 Python dict 传给形参为 `const json&` 的函数即可，pybind11_json 自动转换。

#### 4.3.3 源码精读

**渲染结果 → numpy 的典型实现**：`render_to_cpu` 在 [src/python_api.cu:145-236](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L145-L236)。关键是结尾 [221-235 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L221-L235)：先 `py::array_t<float> result_rgba({height, width, 4})` 与 `result_depth({height, width})`，再用 `cudaMemcpy2DFromArray` / `cudaMemcpy` 把 GPU 数据拷进这两个数组的 `buf.ptr`，最后 `return {result_rgba, result_depth};`。返回类型是 `std::pair<py::array_t<float>, py::array_t<float>>`，pybind11（配合 `<pybind11/stl.h>`）把它转成 Python 的二元组。`render_to_cpu_rgba`（[238-242 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L238-L242)）只是取 `.first`。

**网格 → numpy dict 的典型实现**：`compute_marching_cubes_mesh` 在 [src/python_api.cu:114-142](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L114-L142)。[123-126 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L123-L126)一次性建四个数组 `cpuverts / cpunormals / cpucolors / cpuindices`，分别 `cudaMemcpy(DeviceToHost)`，最后 [141 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L141)用 `py::dict(py::arg("V")=..., py::arg("N")=..., py::arg("C")=..., py::arg("F")=...)` 返回。于是 Python 侧拿到 `{"V": ndarray, "N": ndarray, "C": ndarray, "F": ndarray}`，可直接喂给 `trimesh`/`open3d`。

**numpy → C++（写入训练图像）**：`Testbed::Nerf::Training::set_image` 在 [src/python_api.cu:45-72](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L45-L72)。形参是 `pybind11::array_t<float> img`，第 50 行 `py::buffer_info img_buf = img.request();` 取缓冲，[52-58 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L52-L58)校验 `ndim==3` 且 `shape[2]==4`（即 `(H,W,4)`），再调内部 `dataset.set_training_image(...)`。`override_sdf_training_data`（[74-112 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L74-L112)）是同一套套路接收点云。

**json 互转的入口**：[src/python_api.cu:25](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L25) 包含 `<pybind11_json/pybind11_json.hpp>`，于是 `json` 形参可接 Python dict。两个用到它的绑定：构造函数 `Testbed(ETestbedMode, const fs::path&, const json&)`（[442 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L442)）与 `reload_network_from_json`（[544-550 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L544-L550)）。后者把一个 dict 形式的网络配置直接喂进 u2-l4 / u3-l1 讲的建网流程。

**向量/矩阵的 type_caster**：[include/neural-graphics-primitives/pybind11_vec.hpp:41-81](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/pybind11_vec.hpp#L41-L81) 是 `ngp::tvec<T,N>` 的转换器。`load`（[47-69 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/pybind11_vec.hpp#L47-L69)）从一维 numpy 数组读入，要求长度恰为 `N` 且步长连续；`cast`（[71-77 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/pybind11_vec.hpp#L71-L77)）把向量导出回一维数组。

矩阵转换器 [pybind11_vec.hpp:83-127](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/pybind11_vec.hpp#L83-L127) 多了一个关键细节——**转置**：[第 108 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/pybind11_vec.hpp#L108) `value = ngp::transpose(matrix_type(buf.mutable_data()))`。原因是 numpy 默认行主序，而 GLM 风格的 `tmat` 是列主序；导入时把行主序的 Python 矩阵转置一下，才能得到正确的列主序内部表示。导出时 `cast`（[116-123 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/pybind11_vec.hpp#L116-L123)）则用翻转的 strides 把列主序重新摆成 Python 期望的行主序。这正是 `testbed.set_crop_box(np.array([...]))`、`testbed.camera_matrix = np.array(...)` 能直接吃 numpy 矩阵的底层原因。

#### 4.3.4 代码实践

**实践目标**：体验 numpy 出图与 json 入网两条转换链。

**操作步骤**（**示例代码**，需先有 fox 数据与编译好的 pyngp）：

```python
import pyngp as ngp
import numpy as np

testbed = ngp.Testbed()
testbed.load_training_data("data/nerf/fox")
testbed.shall_train = True
# 训练一小会儿
while testbed.training_step < 500:
    testbed.frame()

# (1) numpy 出图：render 返回 (H,W,4) float numpy 数组
img = testbed.render(width=800, height=600, spp=4)
print(type(img), img.shape, img.dtype)   # 预期 <class 'numpy.ndarray'> (600,800,4) float32

# (2) json 入网：用一个 dict 覆盖网络配置（举例：关掉 jit_fusion）
cfg = testbed.root_dir  # 仅演示读取；真实配置通常从 configs/*.json 读成 dict
# 用 dict 形式重载网络（这里用 base 配置示意，路径相对 root_dir）
# testbed.reload_network_from_json(json_dict, config_base_path="configs/nerf/base.json")
```

**需要观察的现象**：`img.shape == (600, 800, 4)` 且为 `float32`；这说明 `render_to_cpu_rgba` 里的 `py::array_t<float> result_rgba({height, width, 4})` 生效，且最后一维是 RGBA。

**预期结果**：渲染图可正常 `img.astype('uint8')` 后用 `PIL.Image.fromarray(...).save("fox.png")` 存盘（注意 Y 轴朝向、sRGB 转换，可设 `linear=False` 让 C++ 侧做色调映射）。

**待本地验证**：是否需要把 `img` 上下翻转、做 sRGB 编码，取决于你后续用什么库存图；本实践只验证「拿到的确是 numpy 数组」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `compute_marching_cubes_mesh` 返回 `py::dict` 而不是 `std::tuple`？
**答案**：用 `py::dict(py::arg("V")=..., ...)`（[src/python_api.cu:141](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L141)）返回带名字字段的字典 `{"V","N","C","F"}`，调用方写 `mesh["V"]` 比记元组顺序更清晰，也不易错位。

**练习 2**：为什么矩阵 type_caster 要在导入时 `transpose`？
**答案**：因为 numpy 数组按行主序存储，而 ngp 的 `tmat`（GLM 风格）按列主序存储（[pybind11_vec.hpp:106-108](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/pybind11_vec.hpp#L106-L108)）。不转置会导致行列互换，相机/裁剪框矩阵错位。

**练习 3**：`reload_network_from_json(cfg)` 里的 `cfg` 是 Python dict，C++ 形参却是 `const json&`，谁负责转换？
**答案**：`<pybind11_json/pybind11_json.hpp>`（[src/python_api.cu:25](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L25)）为 `nlohmann::json` 注册的 type_caster 负责自动把 Python dict/list/标量转成 `json`。

---

## 5. 综合实践

把三个模块串起来，完成一个端到端任务：**用 pyngp 把 fox 训练 1000 步并保存快照**，同时回答绑定行号。

**第 1 步：定位绑定行**。在 `src/python_api.cu` 的 `PYBIND11_MODULE` 块里找到：
- `render` → [第 507-519 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L507-L519)（绑定 `render_to_cpu_rgba`）
- `load_training_data` → [第 452 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L452)
- `save_snapshot` → [第 563-570 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L563-L570)
- `compute_and_save_marching_cubes_mesh` → [第 592-604 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L592-L604)

**第 2 步：写脚本**（**示例代码**，仿照 `scripts/run.py:94-110,143,207,255` 的写法）：

```python
# train_fox.py —— 放在仓库根目录运行：python train_fox.py
import pyngp as ngp
import os

SCENE = "data/nerf/fox"
N_STEPS = 1000
SNAPSHOT = "fox.ingp"

testbed = ngp.Testbed()                 # ① 构造（mode=None）
testbed.load_training_data(SCENE)       # ② 加载 fox → 自动进入 Nerf 模式
testbed.shall_train = True              # ③ 必须显式开启训练（load_training_data 不会自动开）

while testbed.training_step < N_STEPS:  # ④ 训练 1000 步
    testbed.frame()                     #    一帧 = 训练一步 + 渲染；GIL 在 C++ 内被释放
    if testbed.training_step % 200 == 0:
        print(f"step={testbed.training_step} loss={testbed.loss:.4f}")

os.makedirs(os.path.dirname(SNAPSHOT) or ".", exist_ok=True)
testbed.save_snapshot(SNAPSHOT, False)  # ⑤ 保存快照：不含优化器状态、压缩为 .ingp
print(f"saved {SNAPSHOT}")
```

**第 3 步：验证**。

- **观察现象**：终端每 200 步打印一次 `loss`，`loss` 单调下降（约从几十降到个位数，因为默认 Huber 损失已除以 5，近似 PSNR，见 u4-l4）；运行结束后当前目录出现 `fox.ingp`。
- **预期结果**：`fox.ingp` 可被 `./instant-ngp fox.ingp` 或 `testbed.load_snapshot("fox.ingp")` 重新加载，且 `testbed.training_step` 续在 1000 附近（u6-l3 讲过的「接着训练」语义）。
- **待本地验证**：具体 loss 数值与收敛速度取决于 GPU 与数据，本实践只要求「能跑通且 loss 下降、快照生成」。

**延伸**（可选）：把脚本最后加上网格导出，验证 4.3 的 numpy dict 链路：

```python
mesh = testbed.compute_marching_cubes_mesh(resolution=[256,256,256], thresh=2.5)
print({k: v.shape for k, v in mesh.items()})   # 预期 {'V': (M,3), 'N': (M,3), 'C': (M,3), 'F': (K,3)}
```

## 6. 本讲小结

- `pyngp` 是 instant-ngp 的第二条构建产物，由 `NGP_BUILD_WITH_PYTHON_BINDINGS`（宏 `NGP_PYTHON`）开启；全部绑定集中在 `src/python_api.cu` 的一个 `PYBIND11_MODULE(pyngp, m)` 块里。
- 该块按「枚举 → 自由函数 → 辅助类 → `Testbed` 及嵌套类」顺序注册；枚举用 `py::enum_`、类用 `py::class_`，重载用 `py::overload_cast`，字符串自动转 `fs::path` 靠 `implicitly_convertible`。
- 暴露成员有四档：`def`（方法）、`def_readwrite`（直接读写成员）、`def_readonly`（只读成员）、`def_property[_readonly]`（经 lambda 读写，用于需转换/计算/校验的属性如 `loss`、`dlss`、`root_dir`）。
- 耗时 GPU 操作（`load_training_data`、`frame`、`train`）用 `py::call_guard<py::gil_scoped_release>()` 释放 GIL；直接调 `load_training_data` 不会自动开训练，需手动 `shall_train = True`。
- numpy 互转靠 `py::array_t<float>` + `request()`（如 `render_to_cpu` 出图、`set_image` 入图、`compute_marching_cubes_mesh` 出网格 dict）；JSON 靠 `pybind11_json` 让 `json` 形参接 Python dict；vec/mat 靠 type_caster，矩阵需在导入时转置以消除行/列主序差异。

## 7. 下一步学习建议

- 下一篇 **u7-l2** 会精读 `scripts/run.py`，它把本讲的所有 API 串成一条完整的「训练 → 评测（PSNR/SSIM）→ 截图 → 视频」流水线，是 pyngp 用法的最佳范本。
- 想深入「相机自标定」相关绑定，可对照本讲读 `Testbed::Nerf::Training` 的 `optimize_*` 开关（[src/python_api.cu:788-793](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L788-L793)），并与 u8-l3 的相机/镜头优化对照。
- 想理解 `render_to_cpu` 内部那条「CUDA surface → numpy」链路的下游（CUDA-GL 互操作、动态分辨率），可继续读 u6-l1 的渲染缓冲体系。
- 如果你想扩展绑定（暴露一个新方法），流程是：在 `testbed.h` 加声明 → 在某个 `testbed_*.cu` 实现 → 在 `src/python_api.cu` 的 `Testbed` 链里 `.def(...)` 注册 → 重新编译 pyngp；这正是本项目「应用层」与 tiny-cuda-nn「库层」边界的具体体现（见 u8-l5）。
