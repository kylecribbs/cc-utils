import typing

import tabulate

import clamav.client
import clamav.scan


def as_table(
    scan_results: typing.Iterable[clamav.scan.ClamAV_ResourceScanResult],
    tablefmt: str='simple', # see tabulate module
):
    headers = ('resource', 'status', 'details')

    def row_from_result(scan_result: clamav.scan.ClamAV_ResourceScanResult):
        resource = f'{scan_result.component.name}/{scan_result.artifact.name}'
        res = scan_result.scan_result

        status = res.malware_status

        if status is clamav.client.MalwareStatus.OK:
            details = 'no malware found'
        elif status is clamav.client.MalwareStatus.UNKNOWN:
            details = 'failed to scan'
        elif status is clamav.client.MalwareStatus.FOUND_MALWARE:
            details = '\n'.join((
                f'{finding.name}: {finding.details}' for finding in res.findings
            ))
        else:
            raise NotImplementedError(status)

        return resource, status, details

    def rows():
        for result in scan_results:
            yield row_from_result(scan_result=result)

    return tabulate.tabulate(
        tabular_data=rows(),
        headers=headers,
        tablefmt=tablefmt,
    )
