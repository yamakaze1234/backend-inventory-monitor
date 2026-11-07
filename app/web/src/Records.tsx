import { memo, useState } from 'react';
import { useTable, type ColumnDef, type PaginationState } from '@tanstack/react-table';
import { DataGrid, DataGridContainer, dataGridFeatures, type DataGridFeatures } from '@/components/reui/data-grid/data-grid';
import { DataGridTable } from '@/components/reui/data-grid/data-grid-table';
import { DataGridPagination } from '@/components/reui/data-grid/data-grid-pagination';
import { DataGridScrollArea } from '@/components/reui/data-grid/data-grid-scroll-area';

const i18n = { labels: { rowsPerPage:'每页条数', previousPage:'上一页', nextPage:'下一页', loading:'正在读取记录', empty:'暂无记录',
  paginationInfo:({from,to,count}:{from:number;to:number;count:number})=>`${from}–${to} / 共 ${count} 条`, goToPage:(page:number)=>`第 ${page} 页` } };

/** 复用官方分页示例，稳定行标识，每页最多渲染 25 行而非重建整张表。 */
function RecordsInner<T extends { id?: string; sku?: string }>({ rows, columns, loading, total, pagination, onPaginationChange }:{
  rows:T[]; columns:ColumnDef<DataGridFeatures,T>[]; loading:boolean; total?:number;
  pagination?:PaginationState; onPaginationChange?: (value:PaginationState|((old:PaginationState)=>PaginationState))=>void;
}) {
  const [local, setLocal] = useState({pageIndex:0,pageSize:25});
  const table = useTable({features:dataGridFeatures,data:rows,columns,getRowId:row=>row.id||row.sku||'',
    manualPagination:!!pagination,rowCount:total ?? rows.length,state:{pagination:pagination||local},onPaginationChange:onPaginationChange||setLocal});
  return <DataGrid table={table} recordCount={total??rows.length} isLoading={loading} i18n={i18n} tableLayout={{dense:true}} emptyMessage="暂无符合条件的记录">
    <div className="flex min-w-0 flex-col gap-3"><DataGridContainer><DataGridScrollArea><DataGridTable /></DataGridScrollArea></DataGridContainer><DataGridPagination /></div>
  </DataGrid>;
}
export const Records = memo(RecordsInner) as typeof RecordsInner;
