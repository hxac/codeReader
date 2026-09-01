# Gradio 视频演示：app.py 的会话管理与视频推理

## 1. 本讲目标

读完本讲，你应该能够：

1. 解释 `create_user_temp_dir` / `delete_later` 如何用「UUID 会话目录 + 线程池延迟删除 + atexit 兜底」三件套，解决多用户 Web 演示的文件隔离与磁盘清理问题。
2. 走读 [app.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py) 中 `mesh_inference` 的三个阶段：逐帧前向收集参数 → 参数序列 Savitzky–Golay 平滑 → 逐帧 EHM 重建与渲染，最后编码成浏览器友好的 mp4。
3. 读懂 `gr.Blocks` 界面的组件组成与两条事件绑定（`video_input.change` / `launch_btn.click`），理解 `gr.State` 如何跨事件传递会话信息。
4. 说明 `@spaces.GPU` 装饰器在 HuggingFace Spaces 与本地环境下的不同行为。

本讲是单元二的第四篇。[u2-l2](u2-l2-inference-wo-detect.md) 已经把「最短推理链路」逐行拆开，本讲把这条链路放进一个真实的 Web 服务里，重点不是网络本身（它仍是 `Ehm_Pipeline` → `EHM_v2` → `Renderer2` 那套黑盒），而是**围绕它的工程外壳**：会话管理、视频 IO、时序平滑和界面事件。

## 2. 前置知识

本讲默认你已读过：

- [u2-l2](u2-l2-inference-wo-detect.md)：`pad_and_resize` 预处理、`ehm_model(img_patch)` 前向输出三字段、`GS_Camera` 焦距 24 / 画布 1024 的全局约定。
- [u1-l4](u1-l4-first-inference-run.md)：三个推理入口的产物差异（本讲的产物是 `mesh_video.mp4` 与 `results.npz`）。
- [u2-l1](u2-l1-config-system.md)：`ConfigDict` + `add_extra_cfgs` 读 `configs/infer.yaml`。

再补齐本讲会用到的几组术语：

**Gradio。** 一个纯 Python 的 Web 演示框架，本项目钉死版本 `gradio==4.44.1`（[requirements.txt:22](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/requirements.txt#L22)）。理解 Gradio 只需要三件事：

- **组件（Component）**：`gr.Video`、`gr.Button`、`gr.File`、`gr.Slider`……既是界面元素，也是函数的输入输出容器。
- **布局**：`gr.Blocks` 上下文管理器里用 `gr.Row` / `gr.Column` 摆放组件，纯声明式。
- **事件绑定**：`组件.事件名(fn=处理函数, inputs=[...], outputs=[...])`，把「用户操作」映射到「Python 函数调用」，再把返回值写回组件。

**`gr.State`。** 一种不在界面上显示的隐藏组件，专门用来在**同一次会话的多次事件之间**携带数据。本讲会看到 PEAR 往里面塞了一个 JSON 字符串。

**`ThreadPoolExecutor` 与 `atexit`。** 前者是标准库线程池，`submit(函数)` 把任务丢给后台线程异步执行；后者 `atexit.register(函数)` 注册「进程退出前必执行」的钩子。两者组合实现「延迟删除 + 退出兜底」。

**decord 与 imageio。** 两个视频库分工不同：`decord.VideoReader` 支持按下标随机取帧（`video_reader[i]`），读长视频不必整段载入内存；`imageio` 则包装 ffmpeg 负责转码（截取、重编码）与写出 mp4。

**Savitzky–Golay 滤波（`scipy.signal.savgol_filter`）。** 一种时序平滑方法：对每个时刻 \( t \)，在宽度为 \( w \)（奇数）的窗口内用最小二乘拟合一个 \( p \) 阶多项式

\[
p(\tau) = a_0 + a_1\tau + a_2\tau^2 + \cdots + a_p\tau^p,\quad p < w
\]

再取拟合值 \( p(0) \) 作为该时刻的平滑结果。相比滑动平均，它保住了信号的高阶形状（比如动作的加速度），只压掉噪声。本讲只用到这个直觉，窗口参数的深入实验放在 [u5-l4](u5-l4-temporal-smoothing.md)。

**HuggingFace Spaces 与 `spaces.GPU`。** HuggingFace 的托管平台提供 `spaces` 包，`@spaces.GPU` 装饰器告诉平台「这个函数需要调度到 GPU（ZeroGPU 机制）」。本地开发没有这个包时，代码里写了 fallback——本讲 4.2.3 会指出这个 fallback 其实藏着一个坑。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [app.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py) | 主角：Gradio 视频演示入口，约 870 行 | 会话管理、`mesh_inference`、界面与事件 |
| [utils/pipeline_utils.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/pipeline_utils.py) | 通用工具 | `to_tensor`：numpy 图像转设备上的张量 |
| [models/pipeline/ehm_pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py) | ViT 骨干 + 解码头（[u2-l5](u2-l5-ehm-pipeline-forward.md) 精读） | 本讲当黑盒：吃 `(1,3,256,256)`，吐参数字典 |
| [models/modules/ehm/EHM_v2.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py) | 参数 → 10475 顶点网格（[u4-l4](u4-l4-ehm-v2-fusion.md) 精读） | `forward(body_dict, flame_dict, pose_type='aa')` |
| [models/modules/renderer/body_renderer.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py) | `Renderer2` 渲染器（[u4-l5](u4-l5-mesh-renderer.md) 精读） | `render_mesh(vertices, camera, lights)` |
| [utils/graphics_utils.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py) | 相机 | `GS_Camera` 的 R / T 传法 |
| [configs/infer.yaml](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml) | 推理配置（[u2-l1](u2-l1-config-system.md)） | 只在模块加载期被读一次 |
| [requirements.txt](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/requirements.txt) | 依赖清单 | `gradio==4.44.1`、`imageio`、`decord` |

一个先记住的结构性事实：`app.py` 是**模块级加载**型脚本——`import` 时就完成配置读取、渲染器构建、权重下载与模型实例化（[app.py:127-146](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L127-L146)），因此每次修改代码后重启 demo 的代价不低（要重新走一遍加载）。这会影响第 5 节实践的操作节奏。

---

## 4. 核心概念与源码讲解

先给一张全景图。用户视角只有两步——上传视频、点按钮；代码视角是三个函数接力：

```text
用户上传视频 ──change事件──▶ ① handle_video_upload          app.py:269-341
                              ├─ create_user_temp_dir()       【模块一】会话目录
                              ├─ imageio 截取前 fps×3 帧 → 会话目录/*.mp4
                              ├─ 抽首帧 → base64 + 会话信息
                              └─ return json.dumps(frame_data) ──▶ gr.State
用户点按钮   ──click事件──▶ ② launch_viz                     app.py:515-551
                              ├─ json.loads(state) 取 temp_dir / video_name
                              ├─ ③ mesh_inference(temp_dir, video_name) 【模块二】
                              │    阶段一 逐帧前向收集参数      L378-390
                              │    阶段二 savgol 参数平滑       L393-434
                              │    阶段三 逐帧 EHM+渲染         L439-470
                              │    写 mesh_video.mp4 + results.npz  L477-507
                              ├─ delete_later(temp_dir, 600)   【模块一】再次续期清理
                              └─ return (mesh_video路径, npz路径) ──▶ 右侧视频 + 下载区
```

下面按三个最小模块展开。

### 4.1 模块一：会话临时目录管理

#### 4.1.1 概念说明

把单机推理脚本变成 Web 演示，会立刻冒出两个单机脚本不存在的问题：

1. **文件冲突**：两个用户同时上传视频，如果都写到同一个固定路径（比如 `input.mp4`），后到的会覆盖先到的，先到的用户看到别人的结果。
2. **磁盘泄漏**：每个会话都会产生截取视频、渲染结果，如果只写不删，演示服务跑几天磁盘就满了。

PEAR 的解法是「**每会话一个独立目录 + 定时删除 + 退出兜底**」：

- **独立目录**：上传瞬间用 UUID 生成一个只属于这次会话的目录，所有中间文件都关在里面，天然互不干扰。
- **定时删除**：目录创建时就向后台线程池提交一个「睡 600 秒再删」的任务，用户即使直接关掉网页，10 分钟后文件也会消失。
- **退出兜底**：同时用 `atexit` 注册删除函数，服务进程正常退出时立刻清理，不依赖那个睡着的线程。

#### 4.1.2 核心流程

```text
create_user_temp_dir():
    session_id = uuid4() 前 8 位
    temp_dir   = <app.py所在目录>/temp_local/session_{session_id}
    makedirs(temp_dir)
    delete_later(temp_dir, delay=600)     # ① 线程池: sleep(600) → 删
                                          # ② atexit: 进程退出 → 删
    return temp_dir

一个会话的生命周期:
    t=0    上传 → 目录创建，注册 600s 定时删
    t≈30s  点按钮 → 推理完成 → launch_viz 再注册一次 600s 定时删
    t=600  第一个定时器到期 → 目录被删
    任意时刻进程退出 → atexit 钩子兜底再删一次（目录已不存在也不报错）
```

注意第二次 `delete_later` 并不是「取消旧任务重新计时」，而是**再挂一个新的 600 秒定时器**——旧定时器照样会在 t=600 触发。由于删除函数对「目录不存在」做了容错（`try/except`），重复删除是安全的幂等操作。

#### 4.1.3 源码精读

**线程池是模块级单例**，全脚本共用 2 个后台线程（[app.py:159](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L159)）：

```python
thread_pool_executor = ThreadPoolExecutor(max_workers=2)
```

这句在 [app.py:159](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L159)，为所有延迟删除任务提供执行线程。

**`delete_later`：延迟删除 + 退出兜底的双保险**（[app.py:194-210](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L194-L210)）：

```python
def delete_later(path, delay: int = 600):
    def _delete():
        try:
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        except Exception as e:
            logger.warning(f"Failed to delete {path}: {e}")

    def _wait_and_delete():
        time.sleep(delay)
        _delete()

    thread_pool_executor.submit(_wait_and_delete)   # 路径一：600 秒后删
    atexit.register(_delete)                        # 路径二：进程退出时删
```

这段代码定义了三层防护：`_delete` 统一处理文件与目录两种情况并用 `try/except` 保证重复删除不抛错；`_wait_and_delete` 在后台线程里先睡 `delay` 秒；最后 `submit` 提交延迟任务、`atexit.register` 注册退出钩子，两条路径指向同一个 `_delete`。

**`create_user_temp_dir`：UUID 会话目录**（[app.py:213-226](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L213-L226)）：

```python
session_id = str(uuid.uuid4())[:8]                    # 取 UUID4 前 8 位
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
temp_dir = os.path.join(PROJECT_ROOT, "temp_local", f"session_{session_id}")
os.makedirs(temp_dir, exist_ok=True)
delete_later(temp_dir, delay=600)
```

目录落在**仓库根目录下的 `temp_local/session_xxxxxxxx`**（用 `__file__` 定位而不是当前工作目录，保证从任何位置启动都一致）；`uuid4` 是随机 UUID，取前 8 位足够避免演示规模的撞名。创建完立刻挂 600 秒定时删除。

**推理完成后的第二次清理**（[app.py:536](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L536)）：`launch_viz` 在 `mesh_inference` 返回后再次调用 `delete_later(temp_dir, delay=600)`，相当于「结果交还用户之后再给 10 分钟下载缓冲」。

#### 4.1.4 代码实践

**实践目标**：亲眼看到会话目录的创建与延迟消失，验证 4.1.2 的流程图。

注意：**不要直接 `import app`**——那会触发整份模块级模型加载（下载权重、构建渲染器）。我们把这个机制的最小骨架复制成独立脚本：

```python
# 示例代码：session_probe.py —— 放在仓库根目录运行，观察后删除
import os, time, uuid, shutil, atexit
from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=2)

def delete_later(path, delay=5):                     # 缩短到 5 秒便于观察
    def _delete():
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
        except Exception as e:
            print("delete failed:", e)
    executor.submit(lambda: (time.sleep(delay), _delete()))
    atexit.register(_delete)

def create_user_temp_dir():
    session_id = str(uuid.uuid4())[:8]
    temp_dir = os.path.join("temp_local", f"session_{session_id}")
    os.makedirs(temp_dir, exist_ok=True)
    delete_later(temp_dir, delay=5)
    return temp_dir

d = create_user_temp_dir()
print("created:", d, "| exists:", os.path.exists(d))
for t in range(8):
    time.sleep(1)
    print(f"t={t+1}s exists:", os.path.exists(d))
```

操作步骤：

1. 把上面脚本存为 `session_probe.py`，`python session_probe.py` 运行。
2. 观察输出中 `exists` 何时从 `True` 翻转为 `False`。
3. 把 `delay` 改成 5 → 15，再跑一次，确认翻转时刻随 `delay` 平移。
4. 实验完删除 `session_probe.py` 和可能残留的 `temp_local/`。

需要观察的现象与预期结果：`t=5s` 附近 `exists` 变为 `False`（线程池里的任务睡满 5 秒后执行了删除）；若中途 `Ctrl+C` 杀掉进程，目录不会立刻被删（`atexit` 在 KeyboardInterrupt 时是否执行取决于解释器退出方式），再次运行可手动清理。**运行结果待本地验证**（尤其异常退出路径）。

#### 4.1.5 小练习与答案

**练习 1**：一次完整的「上传 → 点按钮」会话，针对同一个目录总共注册了几个删除钩子？

**答案**：4 个。`create_user_temp_dir` 里一次 `delete_later` 注册了 1 个线程池任务 + 1 个 atexit 钩子；`launch_viz`（[app.py:536](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L536)）又调用一次，再注册 1 + 1。它们全部指向同一目录，靠 `_delete` 的容错保证幂等。

**练习 2**：为什么用 `uuid4` 而不用当前时间戳做会话 ID？

**答案**：时间戳在并发下同一秒会撞名（除非引入计数器或微秒精度），而 `uuid4` 是 122 位随机数，撞名概率可以忽略；且随机 ID 不可预测，避免被猜测路径枚举。截断到 8 位是把「唯一性预算」换成「路径可读性」的工程折中。

**练习 3**：线程池只有 2 个 worker，如果短时间内来了 3 个会话，第 3 个会话的清理会怎样？

**答案**：第 3 个 `_wait_and_delete` 任务会在队列里排队，等前两个任务（各睡 600 秒）之一腾出线程才开始计时，实际删除时间被推后到约 1200 秒。正确性不受影响（删除终会发生），只影响清理的及时性——这是 `max_workers=2` 的隐性代价。

### 4.2 模块二：mesh_inference 主流程

#### 4.2.1 概念说明

`mesh_inference` 是整个 demo 的计算核心（[app.py:343-510](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L343-L510)）。它解决的问题是：**把一段（最多 3 秒的）单人居中视频变成一段重建网格视频**。

它最值得学习的设计是**三阶段两遍**结构：

- **阶段一（第一遍循环）**：逐帧只跑网络 `ehm_model(img_patch)`，把三组输出参数（`body_param` / `flame_param` / `pd_cam`）按帧收集成序列。这一遍**不做** EHM 重建和渲染。
- **阶段二（无循环）**：把每组参数沿时间维拼成 `(T, 1, D)` 张量，用 Savitzky–Golay 滤波做时序平滑。
- **阶段三（第二遍循环）**：用平滑后的参数逐帧 `ehm()` 重建网格并渲染，把渲染帧收集起来编码成 mp4。

为什么必须两遍循环？因为 savgol 滤波是**全局算子**——时刻 \( t \) 的平滑值依赖前后各 \( (w-1)/2 \) 帧，必须先拿到完整序列才能开始。为什么在**参数空间**平滑而不是直接平滑渲染图像？因为参数维数低（几百维）且每一维都有明确物理含义（某个关节的旋转角），平滑后网格自然稳定；而逐帧独立渲染的图像直接做平均会产生「重影」——两帧网格姿态略有差异，叠在一起就是两个半透明人。

#### 4.2.2 核心流程

```text
mesh_inference(temp_dir, video_name):
    渲染器/网络/EHM .to(device)            # 装饰器 @spaces.GPU @torch.no_grad
    video_reader = decord.VideoReader(会话目录/video_name.mp4)

    ── 阶段一：逐帧前向（只收集参数）────────────────
    for i in range(帧数):
        frame = video_reader[i].asnumpy()
        patch = pad_and_resize(frame, 256)              # letterbox 到 256×256
        patch = to_tensor(patch) → /255 → permute → (1,3,256,256)
        outputs = ehm_model(patch)
        收集 outputs['body_param'] / ['flame_param'] / ['pd_cam']

    ── 阶段二：参数序列平滑 ────────────────────────
    body 8 个键:  cat 成 (T,1,D) → savgol(w=7, p=2)
    flame 6 个键: cat 成 (T,1,D) → savgol(w=5, p=2)
    cam:          cat 成 (T,1,4,4) → savgol(w=7, p=2)

    ── 阶段三：逐帧重建渲染 ────────────────────────
    for idx in range(T):
        切第 idx 帧参数 → body_dict（eye_pose/jaw_pose/joints_offset=None）
                        → flame_dict
        pd_smplx_dict = ehm(body_dict, flame_dict, pose_type='aa')
        camera = GS_Camera(焦距24, 画布1024, R=pd_cam[:3,:3], T=pd_cam[:3,3])
        frame_img = body_renderer.render_mesh(vertices, camera, lights)
        all_meshes_img.append(uint8 帧)

    ── 落盘 ────────────────────────────────────────
    imageio 写 mesh_video.mp4（libx264 + yuv420p + faststart，fps=30）
    np.savez_compressed results.npz（faces + vertices）
```

#### 4.2.3 源码精读

**(a) 装饰器与设备准备**（[app.py:343-351](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L343-L351)）：

```python
@spaces.GPU
@torch.no_grad()
def mesh_inference(temp_dir, video_name):
    global body_renderer, ehm_model, ehm
    body_renderer = body_renderer.to(TORCH_DEVICE)
    ...
```

`@spaces.GPU` 让 HuggingFace ZeroGPU 平台把该函数调度到 GPU；`@torch.no_grad()` 关闭梯度追踪省显存；函数体内把三个全局模型搬到 `TORCH_DEVICE`（[app.py:122](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L122) 定义为有 CUDA 用 CUDA 否则 CPU）。

这里有个**从源码可以静态推断出来的坑**：`spaces` 包不在 [requirements.txt](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/requirements.txt) 里。代码里有两处 fallback——[app.py:86-91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L86-L91) 定义的是名为 `space` 的普通函数（拼错名字的死代码），[app.py:109-114](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L109-L114) 定义的是名为 `spaces` 的**普通函数**。普通函数没有 `GPU` 属性，所以本地未安装 `spaces` 包时，[app.py:343](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L343) 的 `@spaces.GPU` 会在**导入期**就抛 `AttributeError`。本地跑 demo 需要先 `pip install spaces`，或临时注释该装饰器（具体报错信息待本地验证）。

**(b) 阶段一：逐帧前向**（[app.py:378-390](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L378-L390)）：

```python
for i in range(len(video_reader)):
    frame = video_reader[i].asnumpy()                  # decord 按下标取帧
    resized = pad_and_resize(frame, target_size=256)
    img_patch = to_tensor(resized, TORCH_DEVICE)
    img_patch = torch.permute(img_patch/255, (2,0,1)).unsqueeze(0)

    outputs = ehm_model(img_patch)

    body_sequence.append(outputs['body_param'])
    flame_sequence.append(outputs['flame_param'])
    cam_sequence.append(outputs['pd_cam'])
```

预处理与 [u2-l2](u2-l2-inference-wo-detect.md) 讲过的完全同构：`pad_and_resize`（[app.py:177-192](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L177-L192)，注意它的默认值是 512，这里显式传 256）做 letterbox；`to_tensor`（[utils/pipeline_utils.py:62-88](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/pipeline_utils.py#L62-L88)）把 numpy 数组变成设备上的张量但**不改变维度顺序**；随后脚本自己完成 `/255`、`permute(2,0,1)` 重排成 `(C,H,W)`、`unsqueeze(0)` 加 batch 维，得到 `(1,3,256,256)`。循环体内没有 `torch.cat`，三个 list 只是按帧追加——注释 `# TODO: Apply EHM processing to frame`（[app.py:380](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L380)）是过时残留，功能早已实现。

**(c) 阶段二：分组平滑**（[app.py:393-434](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L393-L434)）。三组参数、两种窗口：

```python
# body：8 个键，窗口 7
fields1 = ["global_pose", "body_pose", "left_hand_pose", "right_hand_pose",
           "hand_scale", "head_scale", "exp", "shape"]
for key in fields1:
    data_tensor = torch.cat([seq[key] for seq in body_sequence], dim=0)   # (T,1,D)
    processed1[key] = torch.tensor(
        polynomial_smooth(data_tensor, window_size=7, polyorder=2)).cuda()

# flame：6 个键，窗口 5
fields2 = ["eye_pose_params", "pose_params", "jaw_params",
           "eyelid_params", "expression_params", "shape_params"]
for key in fields2:
    data_tensor = torch.cat([seq[key] for seq in flame_sequence], dim=0)
    processed2[key] = torch.tensor(
        polynomial_smooth(data_tensor, window_size=5, polyorder=2)).cuda()

# cam：(T,1,4,4)，窗口 7
cam_sequence = torch.cat(cam_sequence, dim=0)
cam_sequence = torch.tensor(
    polynomial_smooth(cam_sequence, window_size=7, polyorder=2)).cuda()
```

`polynomial_smooth`（[app.py:253-266](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L253-L266)）是 `savgol_filter` 的薄封装：先 `.cpu()` 转 numpy，校验「至少 2 维、窗口为奇数、`polyorder < window_size`」，沿 `axis=0`（时间维）以 `mode='interp'` 处理边界。窗口取值有明显的物理直觉：身体姿态与相机逐帧抖动最伤观感，用大窗 7；FLAME 的表情/眼睛参数本身是高频信号，大窗会把真实眨眼抹平，用保守的 5（源码未写注释，这是从参数语义做的合理推断）。另有两个值得注意的细节：其一，行 421 留着一句协作痕迹注释「这里我猜你原意是从 eye_pose_params 取」，实际代码遍历的是 `flame_sequence` 的每个键，并无错误；其二，`.cuda()` 是**硬编码**的（[app.py:402](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L402)、[L423](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L423)、[L434](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L434)），与上文 `TORCH_DEVICE` 想兼容 CPU 的意图相悖——纯 CPU 机器上即使模型都在 CPU，这里也会报错。

**(d) 阶段三：逐帧重建与渲染**（[app.py:439-470](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L439-L470)）：

```python
for idx in range(global_pose.shape[0]):              # 即 T 帧
    pd_cam = cam_sequence[idx:idx+1]                 # (1,4,4)

    body_dict = { "global_pose": global_pose[idx:idx+1], ..., 
                  'eye_pose': None, 'jaw_pose': None, 'joints_offset': None }
    flame_dict = { "eye_pose_params": ..., "shape_params": ... }

    pd_smplx_dict = ehm(body_dict, flame_dict, pose_type='aa')
    pd_camera = GS_Camera(**build_cameras_kwargs(1, 24),
                          R=pd_cam[0:1,:3,:3], T=pd_cam[0:1,:3,3])
    pd_mesh_img = body_renderer.render_mesh(
        pd_smplx_dict['vertices'][None, 0, ...], pd_camera, lights=lights)
    pd_mesh_img = (pd_mesh_img[:,:3].detach().cpu().numpy()
                   ).clip(0, 255).astype(np.uint8)[0].transpose(1, 2, 0)
    all_meshes_img.append(pd_mesh_img)
```

每个要点都承接前面的讲义：`body_dict` 里 `eye_pose` / `jaw_pose` / `joints_offset` 显式置 `None`，因为眼睛与下颌已由 FLAME 侧的 `eye_pose_params` / `jaw_params` 接管（[u4-l4](u4-l4-ehm-v2-fusion.md) 展开）；相机用 `build_cameras_kwargs(1, 24)`（[app.py:161-173](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L161-L173)）构造「焦距 24、画布 1024、主点 0」的内参，再把平滑后的 `pd_cam` 拆成旋转 `R = pd_cam[:3,:3]` 与平移 `T = pd_cam[:3,3]` 塞给 `GS_Camera`——这正是 [u3-l4](u3-l4-camera-model.md) 要讲的 4×4 RT 约定；渲染输出先切 `[:, :3]`（丢弃 alpha 通道）、再 `clip(0,255)` 转 `uint8`、`transpose(1,2,0)` 从 `(C,H,W)` 回到 `(H,W,C)`。注意 [app.py:472](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L472) 的 `vertices_list.append(...)` 被注释掉了，这就是 [u1-l4](u1-l4-first-inference-run.md) 提到「`results.npz` 里 vertices 为空」的直接原因。

**(e) 视频编码**（[app.py:477-501](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L477-L501)）：

```python
fps = 30
writer = imageio.get_writer(
    mesh_video_path, fps=fps, codec="libx264",
    pixelformat="yuv420p",
    ffmpeg_params=["-movflags", "faststart"],
    macro_block_size=None,
)
for img in all_meshes_img:
    h, w = img.shape[:2]
    img2 = img[: h - (h % 2), : w - (w % 2)]          # yuv420p 要求偶数宽高
    writer.append_data(img2)
```

三个编码参数各有用途：`yuv420p` 是浏览器 HTML5 `<video>` 播放的最稳妥像素格式（默认的 `yuv444p` 在 Safari/Chrome 常黑屏）；`faststart` 把 mp4 索引移到文件头，支持边下边播；偶数宽高裁剪是 `yuv420p` 的硬性要求。失败时回退 `imageio.mimwrite`。**一个源码层面的不一致**：写出 fps 固定为 30（[app.py:483](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L483)），但输入视频的实际 fps 是在**上传阶段**从元数据读的（见 4.3.3）——若上传 25fps 视频，重建视频会以 1.2 倍速播放。

**(f) 结果落盘**（[app.py:505-507](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L505-L507)）：`faces` 取自渲染器的拓扑，`vertices` 因收集语句被注释而为空数组，压缩存成 `results.npz`。

#### 4.2.4 代码实践

**实践目标**：用不依赖 GPU 的最小实验，验证 `polynomial_smooth` 的三条约束，并推断出一个低帧率视频会触发的边界 bug。

```python
# 示例代码：smooth_probe.py —— 放在仓库根目录运行，观察后删除
import numpy as np
import torch
from scipy.signal import savgol_filter

# 复刻 app.py:253-266 的 polynomial_smooth（不依赖模型）
def polynomial_smooth(sequence, window_size=5, polyorder=2):
    seq = np.asarray(sequence.cpu())
    if seq.ndim < 2:
        raise ValueError(f"输入必须至少是 2 维，当前 shape={seq.shape}")
    if window_size % 2 == 0:
        raise ValueError("window_size 必须是奇数")
    if polyorder >= window_size:
        raise ValueError("polyorder 必须小于 window_size")
    return savgol_filter(seq, window_length=window_size,
                         polyorder=polyorder, axis=0, mode='interp')

# 实验 1：正常路径 —— 30fps 视频 3 秒 = 90 帧
ok = polynomial_smooth(torch.randn(90, 1, 6), window_size=7, polyorder=2)
print("90 帧平滑后 shape:", ok.shape)

# 实验 2：偶数窗口 → 应触发自定义校验
for w in (6,):
    try:
        polynomial_smooth(torch.randn(90, 1, 6), window_size=w)
    except ValueError as e:
        print("窗口", w, "→", e)

# 实验 3：低帧率边界 —— fps=2 的视频只有 int(2*3)=6 帧，小于窗口 7
try:
    polynomial_smooth(torch.randn(6, 1, 6), window_size=7, polyorder=2)
except Exception as e:
    print("6 帧 + 窗口 7 →", type(e).__name__, ":", e)
```

操作步骤：

1. 运行 `python smooth_probe.py`。
2. 记录三个实验各自的输出。
3. 对照 [app.py:299](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L299)（`max_frames = int(fps * 3)`）推演：上传一段 fps=2 的视频会发生什么。

需要观察的现象与预期结果：实验 1 输出 `(90, 1, 6)`；实验 2 抛出脚本自定义的「window_size 必须是奇数」；实验 3 中 savgol 收到比窗口还短的序列，预期抛出 `ValueError`（scipy 要求 `window_length <= 序列长度`）——也就是说 **fps 低于 7/3 ≈ 2.34 的视频会让整个 demo 在阶段二崩溃**。scipy 报错原文待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么不能把阶段三合并进阶段一，边推理边渲染？

**答案**：两个独立理由。其一，savgol 是全局算子，第 \( t \) 帧的平滑值需要 \( t \pm 3 \) 帧的参数，序列不完整时无法计算，所以必须先收集完再平滑再使用；其二，分开后第一遍循环只跑网络，显存峰值小（渲染缓冲集中在参数已稳定之后），也方便只对参数做后处理。

**练习 2**：上传 25fps 的视频，输出 `mesh_video.mp4` 的播放速度如何？改动哪一行可以修正？

**答案**：输出固定按 `fps = 30` 写出（[app.py:483](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L483)），90 帧的 25fps 内容（实为 3.6 秒）会被当成 3 秒播放，即 1.2 倍速偏快。修正是把上传阶段读到的真实 fps 通过 `frame_data`（它已经存了 `temp_dir` 等信息）带进 `mesh_inference` 并用于 writer。这正是第 5 节综合实践要打通的「把界面参数传进推理函数」同一套路。

**练习 3**：`body` 组窗口 7、`flame` 组窗口 5，这个差异说明了什么设计考量？

**答案**：窗口越大平滑越强、延迟越高。身体大关节姿态与相机的逐帧噪声让人眼明显感到抖动，值得用大窗压平；而 FLAME 的表情、眼睑、下颌参数对应真实的快速动作（眨眼约 100 毫秒量级），大窗会把真信号当噪声抹掉，因此用更小的 5 保守处理。这是「按信号带宽选窗」的典型取舍（源码本身无注释说明，此为基于参数语义的推断；窗口扫描实验见 [u5-l4](u5-l4-temporal-smoothing.md)）。

### 4.3 模块三：Gradio 界面与事件绑定

#### 4.3.1 概念说明

界面层的任务是把这个两步交互模型讲清楚：

1. **上传即准备**：`video_input` 一旦变化（用户上传或点示例），`change` 事件触发 `handle_video_upload`，它完成会话目录创建、前 3 秒截取、首帧抽取，并把「会话上下文」打包存进隐藏的 `gr.State`。
2. **点击才计算**：`launch_btn` 的 `click` 事件触发 `launch_viz`，它只从 State 里取回 `temp_dir` 与 `video_name`，调用 `mesh_inference`，再把两个产物路径写给右侧视频组件与文件下载组件。

`gr.State` 是连接两次事件的关键：Gradio 的每次事件调用是无状态的（函数不保存上次调用的变量），跨事件共享数据必须显式放在 State 里随会话往返。PEAR 存的是一个 JSON 字符串——把 dict 显式序列化成字符串的好处是可打印、可调试、对队列的多进程环境友好。

#### 4.3.2 核心流程

```text
gr.Blocks(theme=Soft, title="PEAR")
├─ 左列: video_input = gr.Video(format=mp4)          # 输入组件
│        gr.Examples([example_1.mp4, example_2.mp4])  # 示例(仅填路径)
├─ 右列: viz_video = gr.Video(interactive=False)      # 输出组件(只播)
├─ launch_btn / clear_all_btn                         # 按钮
├─ parameters_download = gr.File(interactive=False)   # npz 下载
└─ original_image_state = gr.State(None)              # 隐藏会话状态

事件绑定:
video_input.change(handle_video_upload, [video_input], [state])
    └─ 返回 json.dumps(frame_data) 写入 state
launch_btn.click(launch_viz, [state], [viz_video, parameters_download])
    └─ 返回 (mesh_video路径, npz路径)
```

#### 4.3.3 源码精读

**(a) 上传处理：截取前 3 秒并打包会话上下文**（[app.py:269-341](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L269-L341)）。核心是 [app.py:294-308](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L294-L308) 这段：

```python
input_source = video if isinstance(video, str) else video.name
video_name = get_video_name(input_source)
video_path = os.path.join(user_temp_dir, f"{video_name}.mp4")

reader = imageio.get_reader(input_source)
meta_data = reader.get_meta_data()
fps = meta_data.get('fps', 30)                 # 源视频真实帧率，缺省 30
max_frames = int(fps * 3)                      # 「前 3 秒」的硬编码就在这里
writer = imageio.get_writer(video_path, fps=fps, codec='libx264', quality=8)

for i, frame in enumerate(reader):
    if i >= max_frames:
        break
    writer.append_data(frame)
```

Gradio 4.x 的 `gr.Video` 上传后传给处理函数的是一个临时文件路径字符串（代码同时兼容旧版的文件对象）。这里用 imageio **重编码**而不是复制文件：读到第 `int(fps*3)` 帧就 break，产出一份最长 3 秒的 mp4 落在会话目录里。随后的 [app.py:318-341](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L318-L341) 用 OpenCV 抽第一帧、把短边等比缩放到 336（宽高取偶）、连同 `temp_dir` / `video_name` / `video_path` 一起 `json.dumps` 返回。一个阅读发现：首帧的 base64 数据（`frame_data['data']`）在下游**从未被消费**——`launch_viz` 只取 `temp_dir` 和 `video_name`（[app.py:524-525](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L524-L525)），应是早期「首帧 3D 预览」功能的遗留。

**(b) 界面组件**（[app.py:558-806](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L558-L806)）：

- `gr.Blocks(theme=gr.themes.Soft(), title=..., css=...)`（[app.py:558-727](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L558-L727)）：约 170 行 CSS 几乎都在美化 `gr.Examples`——把示例区改成横向滚动卡片（`elem_classes=["horizontal-examples"]` 配合 `[data-testid="examples"]` 选择器）、钉死上传与结果视频的高度。CSS 不影响逻辑，初读可整体跳过。
- 输入组件 `video_input = gr.Video(format="mp4", height=250, elem_id="video_input")`（[app.py:753-758](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L753-L758)）。
- 示例区 `gr.Examples(examples=[["example/example_1.mp4"], ["example/example_2.mp4"]], inputs=[video_input], fn=None, cache_examples=False, examples_per_page=6)`（[app.py:766-777](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L766-L777)）：`fn=None` 表示示例**只把路径填进组件**不做任何计算，填入后自然触发 `video_input.change` 走正常上传流程；`cache_examples=False` 避免服务启动时预跑推理。
- 输出组件 `viz_video = gr.Video(interactive=False, autoplay=False, sources=None)`（[app.py:784-791](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L784-L791)）：`interactive=False` + `sources=None` 把它变成纯播放器，隐藏上传/摄像头按钮。
- 按钮 `launch_btn`（primary 大按钮）与 `clear_all_btn`（[app.py:794-798](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L794-L798)）；下载区 `parameters_download = gr.File(interactive=False)`（[app.py:803-806](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L803-L806)）。
- 隐藏状态 `original_image_state = gr.State(None)`（[app.py:849](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L849)）。

**(c) 两条事件绑定**（[app.py:854-866](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L854-L866)）：

```python
video_input.change(
    fn=handle_video_upload,
    inputs=[video_input],
    outputs=[original_image_state],
    api_name=False
)

launch_btn.click(
    fn=launch_viz,
    inputs=[original_image_state],
    outputs=[viz_video, parameters_download],
    api_name=False
)
```

这是全文件最关键的 12 行：`change` 的返回值（JSON 字符串）被写进 `State`，`click` 再把它作为唯一输入读出来——数据在两次事件之间只经由 State 流动。`api_name=False` 表示不为该事件生成公开 API 端点（注释标了「⭐ 关键」），避免外部脚本绕过界面直接调用吃 GPU 的推理接口。

**(d) 启动**（[app.py:870-873](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L870-L873)）：`demo.queue().launch()`。`queue()` 必不可少——推理一次几十秒，不加队列时并发请求会互相踩踏，加队列后按序处理并给用户排队反馈。

**(e) 三个阅读发现**（都可以仅靠读码确认）：

1. `clear_all_btn` **没有任何事件绑定**——界面上点「🗑️ Clear All」不会有任何反应。
2. 首帧 base64（`frame_data['data']`）无消费者，属遗留字段。
3. 状态提示文案写死「only supports single human-centered video inputs (3 seconds)」（[app.py:744](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L744)），与 `max_frames = int(fps * 3)` 的硬编码互为印证——这正是第 5 节实践要把它参数化的目标。

#### 4.3.4 代码实践

**实践目标**：给「无处理器的按钮」接上行为，练习「组件 → 事件 → 函数 → 组件」的完整绑定闭环。

操作步骤：

1. 打开 [app.py:861-866](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L861-L866)，在 `launch_btn.click(...)` 之后追加：

```python
# 示例代码：接管 Clear All 按钮（加在 app.py 的 launch_btn.click 之后）
def clear_all():
    return None, None, None, None          # 依次对应下面 4 个 outputs

clear_all_btn.click(
    fn=clear_all,
    inputs=None,
    outputs=[video_input, viz_video, parameters_download, original_image_state],
    api_name=False,
)
```

2. 先 `pip install spaces`（否则见 4.2.3 (a) 的导入期报错），再从仓库根目录 `python app.py`。
3. 打开浏览器，上传 `example/example_1.mp4`，点「Start Tracking Now!」等结果出来，再点「Clear All」。

需要观察的现象与预期结果：四个组件同时清空——上传区变空、结果视频消失、下载区消失，且再次上传能重新走通全流程（State 被清成 `None`，`launch_viz` 开头的 `if original_image_state is None: return None, None` 分支会在未上传时拦住误点）。若点 Clear 无反应，检查绑定是否写在了 `with gr.Blocks(...) as demo:` 块**内部**（缩进层级错了组件不可见）。运行行为待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`handle_video_upload` 的返回值去哪了？为什么界面看不到它？

**答案**：`video_input.change` 的 `outputs=[original_image_state]` 把 JSON 字符串写进了隐藏组件 `gr.State`，它本来就不渲染在界面上；用户看到的「上传完成」反馈只是 `gr.Video` 组件自身显示了视频。下一次事件 `launch_btn.click` 再以 `inputs=[original_image_state]` 取回。

**练习 2**：为什么 `gr.Examples` 传 `fn=None` 也能触发后续流程？

**答案**：`fn=None` 时示例点击只把样本路径填入 `inputs` 指定的组件（`video_input`），不做任何计算；而路径填入会改变 `video_input` 的值，从而触发它的 `change` 事件，走和手动上传完全相同的 `handle_video_upload` 流程。`cache_examples=False` 则保证不在启动时预计算。

**练习 3**：`api_name=False` 去掉了什么？

**答案**：Gradio 默认为每个绑定的事件生成一个公开的 HTTP API 端点（可在 `http://<host>:<port>/?view=api` 查看并直接调用）。设为 `False` 后这两个事件不出现在 API 文档里，外部无法绕过界面直接调用 `launch_viz` 占用 GPU——对部署在公网的演示服务是一层基本保护。

---

## 5. 综合实践

把两处硬编码变成用户可控参数：**「截取前 3 秒」改成 `gr.Slider`（1~5 秒）**，**「是否启用时序平滑」改成 `gr.Checkbox`**。这个任务会同时打通「界面组件 → 事件 inputs → 处理函数签名 → 推理函数行为」整条链路，是本讲三个模块的综合。

> 提醒：以下修改都在你的本地副本上做实验，不要提交；改完需重启 `python app.py`（模型是模块级加载，重启有加载成本）。

**第 1 步：加组件。** 在 `video_input = gr.Video(...)`（[app.py:753-758](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L753-L758)）之后追加：

```python
# 示例代码
duration_slider = gr.Slider(minimum=1, maximum=5, step=1, value=3,
                            label="截取时长（秒）")
```

在 `launch_btn` 所在的 `gr.Row` 里（[app.py:794-798](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L794-L798)）追加：

```python
# 示例代码
smooth_checkbox = gr.Checkbox(value=True, label="启用参数时序平滑")
```

**第 2 步：让秒数进入 `max_frames`。** 修改 `handle_video_upload` 的签名与硬编码行（[app.py:269](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L269)、[L299](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L299)）：

```python
# 示例代码
def handle_video_upload(video, duration=3):          # 新增形参
    ...
    max_frames = int(fps * duration)                 # 原来是 int(fps * 3)
```

并更新事件绑定，同时让滑块变化也重新触发上传流程（否则用户先传视频再拖滑块不会生效）：

```python
# 示例代码
video_input.change(fn=handle_video_upload,
                   inputs=[video_input, duration_slider],
                   outputs=[original_image_state], api_name=False)
duration_slider.change(fn=handle_video_upload,
                       inputs=[video_input, duration_slider],
                       outputs=[original_image_state], api_name=False)
```

注意：`duration_slider.change` 触发时 `video_input` 可能为空，`handle_video_upload` 开头已有的 `if video is None: return None` 分支正好兜住。

**第 3 步：让平滑开关进入 `mesh_inference`。** 平滑发生在推理函数里，而它只被 `launch_viz` 调用，所以要穿三层：

```python
# 示例代码 —— 三个函数各改一处

def mesh_inference(temp_dir, video_name, use_smooth=True):      # ① 加形参
    ...
    def _smooth(t, window):                                     # ② 统一平滑出口
        if use_smooth:
            return torch.tensor(polynomial_smooth(t, window_size=window,
                                                  polyorder=2)).cuda()
        return t
    # ③ 三处替换：
    # processed1[key] = _smooth(torch.cat(data_list, dim=0), 7)
    # processed2[key] = _smooth(torch.cat(data_list, dim=0), 5)
    # cam_sequence   = _smooth(cam_sequence, 7)

def launch_viz(original_image_state, use_smooth=True):
    ...
    mesh_inference(temp_dir, video_name, use_smooth=use_smooth)

launch_btn.click(fn=launch_viz,
                 inputs=[original_image_state, smooth_checkbox],   # ④ 加输入
                 outputs=[viz_video, parameters_download], api_name=False)
```

关闭平滑时直接返回 `torch.cat` 的原序列（它本来就在 GPU 上），后续 `idx:idx+1` 切片逻辑不变。

**第 4 步：运行验证。** 重启 demo，用 `example/example_1.mp4` 做四组实验：

| 组 | 滑块 | 平滑 | 需要观察的现象 | 预期结果 |
| --- | --- | --- | --- | --- |
| A | 3 秒 | 开 | 基线 | 与原版 demo 一致 |
| B | 1 秒 / 5 秒 | 开 | 结果时长 | 1 秒明显更短、5 秒更长（前提是源视频够长，不足 5 秒则整段处理） |
| C | 3 秒 | 关 | 网格稳定性 | 手部、头部出现逐帧细微抖动/漂移，与 A 对比明显 |
| D | 拖滑块后再点按钮 | 开 | 事件链 | 滑块值生效，说明 `duration_slider.change` 重新写了 State |

同时检查会话目录（`temp_local/session_*`）里的截取视频时长随滑块变化，并确认 10 分钟后被自动清理。

**预期结果与回滚**：四组都符合预期即实践成功。做完后用 `git checkout -- app.py`（或手动还原四处修改）恢复原样。全部运行结果待本地验证。

---

## 6. 本讲小结

- `app.py` 用「UUID 会话目录 + `ThreadPoolExecutor` 延迟删除 + `atexit` 兜底」三件套解决多用户 Web 演示的文件隔离与磁盘清理；一次完整会话对同一目录注册 4 个清理钩子，靠容错实现幂等。
- `mesh_inference` 是三阶段两遍结构：先逐帧前向**只收集参数**，再在参数空间做 Savitzky–Golay 平滑（body/cam 窗口 7、flame 窗口 5），最后逐帧 `ehm()` 重建 + `GS_Camera` + `render_mesh` 渲染并编码 mp4——在参数空间而非图像空间平滑，是为了避免多帧网格叠加的重影。
- 视频编码三件套 `libx264 + yuv420p + faststart` 是浏览器兼容的关键，且宽高必须裁成偶数；写出 fps 硬编码 30 与上传阶段读取的真实 fps 不一致，非 30fps 源会变速播放。
- 界面是两步事件模型：`video_input.change → handle_video_upload → gr.State(JSON)` 准备会话，`launch_btn.click → launch_viz → mesh_inference` 执行计算；`gr.State` 是跨事件传数据的唯一桥梁。
- 读码发现的四处硬伤/遗留：本地未装 `spaces` 包时 `@spaces.GPU` 导入期即报错；fps 低于约 2.34 的视频会让 savgol 窗口大于序列长度而崩溃；`clear_all_btn` 没有绑定任何事件；首帧 base64 字段无消费者。

## 7. 下一步学习建议

- 下一讲 [u2-l5](u2-l5-ehm-pipeline-forward.md) 打开本讲一直当黑盒的 `Ehm_Pipeline.forward`：归一化、256×192 裁剪、backbone/head 调用顺序与权重加载约定，补全单元二的最后一块拼图。
- 想深入平滑参数：[u5-l4](u5-l4-temporal-smoothing.md) 会对 `polynomial_smooth` 做窗口扫描实验（3/7/21），定量对比抖动抑制与动作延迟的取舍。
- 修改界面后建议快速过一遍 Gradio 4.x 文档中 [Blocks 与事件监听](https://www.gradio.app/guides/blocks-and-event-listeners) 一节，重点看 `change` / `click` / `inputs` / `outputs` / `queue` 的语义，对照本讲 4.3 的绑定代码阅读效率最高。
