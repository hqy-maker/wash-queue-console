# LibTV CLI 操作与画布排版参考

## 1. 环境绑定与常用查询

```bash
# 检查登录态与目录绑定（若 CLI 报 401/未授权，用 libtv login web + chrome-devtools 在已登录 Chrome 打开 callback 并关掉临时页续期）
libtv status
libtv account info

# 列出工作区与画布
libtv workspace list
libtv project list -s 10

# 新建画布并绑定当前工作目录（写入 .libtv/project.json）
libtv project create "<画布名称>" -w <workspaceId>
libtv project use <画布UUID>
```

> 画布网页直达链接：`https://www.liblib.tv/canvas?projectId=<画布UUID>`

---

## 2. `Seedance 2.5（样片模式）` 关键 Schema 摘要

（以 `libtv model "Seedance 2.5（样片模式）"` 实测为准）

- **`model` 传参名**：`"Seedance 2.5（样片模式）"`（`modelKey: star-video2.5-draft`）
- **`modeType`**：`mixed2video`（全能参考；最多 10 视频 + 30 图片 + 10 音频，总素材 1–50 个）
- **`resolution`**：`480p`（样片模式仅支持 480P）
- **`ratio`**：`adaptive` | `16:9` | `4:3` | `1:1` | `3:4` | `9:16` | `21:9`（**本流程默认 `21:9`**）
- **`duration`**：整数 `4`–`30`（**本流程默认对齐原片最接近整数秒**；计费提示：参考视频时长计入总时长）
- **`enableSound`**：`on` | `off`（**本流程默认 `on`**）
- **Seedance 本地图片预检硬要求**：
  - 格式：`jpeg` / `png` / `webp` / `bmp` / `tiff` / `gif` / `heic`
  - 边长：`300–6000px`
  - 宽高比：`0.4–2.5`
  - 大小：`< 30MB`

---

## 3. 防重名与网格排版模板

### 命名约定（避免 `{{Node "节点名"}}` 重名报错）
`{{Node "节点名"}}` 在写入时按**整张画布**精确匹配展示名，多镜重名会直接中止写入。

- 公共资产分组 `公共角色资产`：如 `公共-主角A三视图`、`公共-主角B定妆`
- 单镜分组 `S01_<简短镜名>`：
  - `S01-原片`（上传原视频）
  - `S01-构图图`（上传构图/影调参考图）
  - `S01-角色图`（仅当该镜独有时上传；复用公共资产则不重复上传）
  - `深度动作捕捉-S01-原片`（网页点击「深度动作捕捉」生成，**连线时优先使用其 `nodeKey`**）
  - `S01-样片`（`Seedance 2.5（样片模式）` 视频生成节点）

### 推荐坐标网格（严格从左往右延展）
- **写入已有画布时**：先拉取 `libtv project <画布UUID>` 算出已有节点右边界 `max_right = max(node.position.x + node.width)`，新分组起点取 `base_x = max_right + 400`，向右顺排。
- **单镜内部从左往右流动**：
  1. 最左列（`x=0`）：参考素材（构图图、角色图纵列排布）+ 原视频（`x=900`）
  2. 中间（由网页「深度动作捕捉」自动落在原片右侧，约 `x=1800`）
  3. 右侧（`x=2850`，景深节点右缘留约 `200px` 间距）：`S01-样片`（`Seedance 2.5（样片模式）` 节点）
  4. 最右（`x=3750`）：后续 `libtv video final` 生成的 1080P 正片会自动接在样片右侧

```bash
# 1) 创建本镜普通分组并切换目录绑定（或显式传 -g S01）
libtv group create "S01"

# 2) 上传本镜原片与独有参考图（从左往右：参考图列 -> 原片）
libtv upload "S01-构图图" -t image -f ./shots/S01/comp.png -g "S01" --x <base_x> --y 0
libtv upload "S01-角色图" -t image -f ./shots/S01/char.png -g "S01" --x <base_x> --y 420
libtv upload "S01-原片" -t video -f ./shots/S01/src.mp4 -g "S01" --x <base_x + 900> --y 0

# 3) 通过 chrome-devtools MCP 在用户已登录 Chrome 中打开画布，找到「S01-原片」：
#    Step A: 点击「深度动作捕捉」生成节点（默认 720 P，此时尚未开始计算）
#    Step B: 必须再点击新节点工具栏「确认提取」，等待生成完成（如渲染出 2522 × 1080 / 0:07）
# 4) 查出景深节点 nodeKey（source == S01-原片.id / isDepthMapRefOutput == true 且 status == 2）并纳入分组 S01：
libtv project <画布UUID>
libtv group "S01" --node "<深度节点nodeKey>"

# 5) 建 Seedance 2.5 样片节点（落在景深节点右侧，建议 x = 景深.x + 景深.width + ~200；仅连参考图 + 景深视频，不连原片）
# ⚠️ 顺序铁律：libtv CLI 写 mixedListOrder 时固定把图片排在前(1..K)、视频排在后(K+1)！
#    因此 --left 与 --prompt 首句必须统一严格按「构图图(图片1) -> 角色图(图片2..K) -> 景深视频(视频K+1)」顺排！
# ⚠️ --x/--y 必须写在 create 前；--run 默认不传；含多行中文提示词时建议用 uv run --no-project python subprocess 传参：
libtv node --x <base_x + 2850> --y <top_y> create "S01-样片" -t video -g "S01" \
  -s "model=Seedance 2.5（样片模式）" \
  -s modeType=mixed2video \
  -s ratio=21:9 \
  -s resolution=480p \
  -s duration=5 \
  -s enableSound=on \
  --left "<构图图nodeKey>" --left "<角色图nodeKey>" --left "<深度节点nodeKey>" \
  --prompt "{{Node <构图图nodeKey>}} ：构图与影调参考。 {{Node <角色图nodeKey>}} ：人物形象参考。 {{Node <深度节点nodeKey>}} ：动作表演、节奏与运镜参考。 ……"
# （仅当用户明确要求自动代跑时才在末尾追加 --run）
# 6) 写完后在 chrome-devtools MCP 中刷新画布并选中 S01-样片，确认顶部 1..N 缩略图与正文 @图片1..@视频N 一一对应后再交由用户点生成
```

> **提示**：提示词中建议一律用 `{{Node <nodeKey>}}`（由脚本或协调者替换填入），既可跨分组引用公共资产，也彻底免疫同名节点导致的写入中断。

---

## 4. 自检拉片、转 1080P 正片与批量下载

```bash
# 用户在画布点击生成并出片后，下载单镜样片回本地自检（VIP 会员传 --watermark false；非会员传 true）
libtv download -n "S01-样片" -g "S01" --watermark false -o ./work/drafts

# 样片修改更新（无需删节点，改提示词/连线后默认留待用户在画布确认后点生成；仅当用户明确要求代跑时再追加 --run）
libtv node "S01-样片" -g "S01" --prompt "<修改后的提示词>"

# 确认 OK 后，7 天内一键转 1080P 正片（会在右侧新建 1080P 节点并自动连线）
libtv video final -n "S01-样片" -g "S01"

# 批量打包下载整组产物
libtv download -n "S01" --watermark false -o ./work/finals
```

---

## 5. 构图图最小必要局部涂白（`scripts/whiteout_mask.py`）

当构图/影调参考图与角色图局部对不上（且确定不是整人替换场景）时，只把错位五官/帽饰/系带等**最小必要局部**涂白 `#FFFFFF`，保住衣领、肩背、衣服褶皱的受光面与投影：

```bash
# 1) 生成 0..1000 网格辅助图（人物较小时加 --crop x1,y1,x2,y2 放大）
uv run --offline ./scripts/whiteout_mask.py grid \
  -i ./shots/S01/comp.jpg -o ./work/S01_grid.jpg [--crop 480,0,760,900]

# 2) 按网格顶点写入 mask.json（pad_px 默认 2，不吞衣领高光），涂白输出无损 PNG
uv run --offline ./scripts/whiteout_mask.py apply \
  -i ./shots/S01/comp.jpg -o ./work/S01_comp_masked.png --spec ./work/S01_mask.json

# 3) 视觉复查 ./work/S01_comp_masked.png 无漏边且保住身体光影后，再过预检并上传为 S01-构图图
uv run --no-project python ./scripts/preflight_check.py ./work/S01_comp_masked.png
```
