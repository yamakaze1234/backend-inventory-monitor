"""技嘉单店 CWS 试点；只导出官方商品表，不写工作台库存或业务配置。"""
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Annotated

from scraper.script_api import ScriptBase


@dataclass(frozen=True)
class Row:
    店铺: Annotated[str, '官方报表核验店名']
    商品ID: Annotated[str, '商品ID']
    SKU: Annotated[str, '规格ID（SKUID）']
    库存: Annotated[int | None, '现货可售+预售库存+阶梯库存']
    类目: Annotated[str, '一级类目/二级类目/三级类目/四级类目']
    商品总数: Annotated[int, '商品管理全部标签与导出数量交叉核验']
    报表时间: Annotated[str, '官方报表任务时间']
    报表文件: Annotated[str, '本次已校验下载文件']


class Script(ScriptBase):
    platform = 'DY'
    description = '技嘉旗舰店官方商品表导出，校验全部商品覆盖后仅返回整机 SKU 库存'
    tags = ['库存', '巡检']
    home_url = 'https://fxg.jinritemai.com/ffa/g/list'

    def run(self, 状态目录: str) -> list[Row]:
        """独立目录保存待下载任务，异常后续跑同一任务，防止重复导出。"""
        support = Path(__file__).resolve().parents[3] / 'support'
        sys.path.insert(0, str(support))
        from douyin_cws_export import CwsExportWorker
        worker = CwsExportWorker(self, 状态目录, '技嘉旗舰店')
        worker.call('login')
        result = worker.call('collect')
        evidence = result['export']
        return [Row('技嘉旗舰店', p['product_id'], s['sku_id'], s['export_sellable'],
                    p['category'], evidence['product_count'], evidence['report_time'], evidence['file'])
                for p in result['products'] for s in p['skus']]
