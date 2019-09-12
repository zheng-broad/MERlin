import numpy as np
import pandas
from scipy import optimize

from merlin.core import analysistask
from merlin.util import barcodedb
from merlin.data.codebook import Codebook


class FilterBarcodes(analysistask.ParallelAnalysisTask):

    """
    An analysis task that filters barcodes based on area and mean
    intensity.
    """

    def __init__(self, dataSet, parameters=None, analysisName=None):
        super().__init__(dataSet, parameters, analysisName)

        if 'area_threshold' not in self.parameters:
            self.parameters['area_threshold'] = 3
        if 'intensity_threshold' not in self.parameters:
            self.parameters['intensity_threshold'] = 200
        if 'distance_threshold' not in self.parameters:
            self.parameters['distance_threshold'] = 1e6

    def fragment_count(self):
        return len(self.dataSet.get_fovs())

    def get_barcode_database(self):
        return barcodedb.PyTablesBarcodeDB(self.dataSet, self)

    def get_estimated_memory(self):
        return 1000

    def get_estimated_time(self):
        return 30

    def get_dependencies(self):
        return [self.parameters['decode_task']]

    def get_codebook(self):
        decodeTask = self.dataSet.load_analysis_task(
            self.parameters['decode_task'])
        return decodeTask.get_codebook()

    def _run_analysis(self, fragmentIndex):
        decodeTask = self.dataSet.load_analysis_task(
                self.parameters['decode_task'])
        areaThreshold = self.parameters['area_threshold']
        intensityThreshold = self.parameters['intensity_threshold']
        distanceThreshold = self.parameters['distance_threshold']
        barcodeDB = self.get_barcode_database()
        currentBC = decodeTask.get_barcode_database() \
            .get_filtered_barcodes(areaThreshold, intensityThreshold,
                                   distanceThreshold=distanceThreshold,
                                   fov=fragmentIndex)
        barcodeDB.write_barcodes(currentBC, fov=fragmentIndex)


class GenerateAdaptiveThreshold(analysistask.AnalysisTask):

    """
    An analysis task that generates a three-dimension mean intenisty,
    area, minimum distance hisogram for barcodes as they are decoded.
    """

    def __init__(self, dataSet, parameters=None, analysisName=None):
        super().__init__(dataSet, parameters, analysisName)

    def fragment_count(self):
        return len(self.dataSet.get_fovs())

    def get_estimated_memory(self):
        return 5000

    def get_estimated_time(self):
        return 1800

    def get_dependencies(self):
        return [self.parameters['decode_task']]

    def get_blank_count_histogram(self) -> np.ndarray:
        return self.dataSet.load_analysis_result('blank_counts', self)

    def get_coding_count_histogram(self) -> np.ndarray:
        return self.dataSet.load_analysis_result('coding_counts', self)

    def get_total_count_histogram(self) -> np.ndarray:
        return self.get_blank_count_histogram() \
               + self.get_coding_count_histogram()

    def get_area_bins(self) -> np.ndarray:
        return self.dataSet.load_analysis_result_if_available('area_bins', self)

    def get_distance_bins(self) -> np.ndarray:
        return self.dataSet.load_analysis_result_if_available(
            'distance_bins', self)

    def get_intensity_bins(self) ->np.ndarray:
        return self.dataSet.load_analysis_result('area_bins', self, None)

    def get_blank_fraction_histogram(self) -> np.ndarray:
        blankHistogram = self.get_blank_count_histogram()
        totalHistogram = self.get_coding_count_histogram()
        blankFraction = blankHistogram / totalHistogram
        blankFraction[totalHistogram == 0] = 0
        return blankFraction

    def calculate_threshold_for_misidentification_rate(
            self, targetMisidentificationRate: float) -> float:
        """ Calculate the blank fraction threshold that achieves a specified
        misidentification rate.

        Args:
            targetMisidentificationRate: the target misidentification rate
        Returns: the blank fraction threshold that achieves
            targetMisidentificationRate
        """
        decodeTask = self.dataSet.load_analysis_task(
            self.parameters['decode_task'])
        codebook = decodeTask.get_codebook()
        blankBarcodeCount = len(codebook.get_blank_indexes())
        codingBarcodeCount = len(codebook.get_coding_indexes())

        blankHistogram = self.get_blank_count_histogram()
        codingHistogram = self.get_coding_count_histogram()
        blankFraction = self.get_blank_fraction_histogram()

        def misidentification_rate_error_for_threshold(x, targetError):
            selectBins = blankFraction > x
            codingCounts = np.sum(codingHistogram[selectBins])
            blankCounts = np.sum(blankHistogram[selectBins])

            return ((blankCounts/blankBarcodeCount) /
                    (codingCounts/codingBarcodeCount)) - targetError
        return optimize.newton(
            misidentification_rate_error_for_threshold, 0.2,
            args=[targetMisidentificationRate], tol=0.001, x1=0.3)

    def extract_barcodes_with_threshold(self, blankThreshold: float,
                                        barcodeSet: pandas.DataFrame
                                        ) -> pandas.DataFrame:
        selectData = barcodeSet[
            ['mean_intensity', 'min_distance', 'area']].values
        selectData[:, 0] = np.log10(selectData[:, 0])
        blankFractionHistogram = self.get_blank_fraction_histogram()

        def bounded_digitize(inputArray: np.ndarray, bins: np.ndarray,
                             right: bool=False) -> np.ndarray:
            inputArray[inputArray<=bins[0]] = bins[0] + 1e-10
            inputArray[inputArray>=bins[1]] = bins[1] - 1e-10
            return np.digitize(inputArray, bins, right)
        barcodeBins = np.array(
            (bounded_digitize(selectData[:, 0], self.get_intensity_bins(),
                              right=True),
             bounded_digitize(selectData[:, 1], self.get_distance_bins(),
                              right=True),
             bounded_digitize(selectData[:, 2], self.get_area_bins()))) - 1
        raveledIndexes = np.ravel_multi_index(
            barcodeBins[:, :], blankFractionHistogram.shape)

        thresholdedBlankFraction = blankFractionHistogram < blankThreshold
        return barcodeSet[np.take(thresholdedBlankFraction, raveledIndexes)]

    @staticmethod
    def _extract_counts(barcodes, intensityBins, distanceBins, areaBins):
        barcodeData = barcodes[
            ['mean_intensity', 'min_distance', 'area']].values
        barcodeData[:, 0] = np.log10(barcodeData[:, 0])
        return np.histogramdd(
            barcodeData, bins=(intensityBins, distanceBins, areaBins))[0]

    def _run_analysis(self):
        decodeTask = self.dataSet.load_analysis_task(
            self.parameters['decode_task'])
        codebook = decodeTask.get_codebook()
        barcodeDB = decodeTask.get_barcode_database()

        completeFragments = \
            self.dataSet.load_numpy_analysis_result_if_available(
                'complete_fragments', self, [False]*self.fragment_count())
        pendingFragments = [
            decodeTask.is_complete(i) and not completeFragments[i]
            for i in range(self.fragment_count())]

        areaBins = self.dataSet.load_analysis_result_if_available(
            'area_bins', self, np.arange(35))
        distanceBins = self.dataSet.load_analysis_result_if_available(
            'distance_bins', self,
            np.arange(
                0, decodeTask.parameters['distance_threshold']+0.02, 0.01))
        intensityBins = self.dataSet.load_analysis_result_if_available(
            'area_bins', self, None)

        blankCounts = self.dataSet.load_analysis_result_if_available(
            'blank_counts', self, None)
        codingCounts = self.dataSet.load_analysis_result_if_available(
            'coding_counts', self, None)

        self.dataSet.save_numpy_analysis_result(
            areaBins, 'area_bins', self)
        self.dataSet.save_numpy_analysis_result(
            distanceBins, 'distance_bins', self)

        updated = False
        while not all(completeFragments):
            if intensityBins is None:
                for i in range(self.fragment_count()):
                    if not pendingFragments[i] and decodeTask.is_complete(i):
                        pendingFragments[i] = decodeTask.is_complete(i)

                if np.sum(pendingFragments) > min(20, self.fragment_count()):
                    def extreme_values(inputData: pandas.Series):
                        return inputData.min(), inputData.max()
                    intensityExtremes = [
                        extreme_values(barcodeDB.get_barcodes(
                            i, columnList=['mean_intensity'])['mean_intensity'])
                        for i, p in enumerate(pendingFragments) if p]
                    maxIntensity = np.max([x[1] for x in intensityExtremes])
                    intensityBins = np.arange(0, 2 * maxIntensity,
                                              maxIntensity / 100)
                    self.dataSet.save_numpy_analysis_result(
                        intensityBins, 'intensity_bins', self)

                    blankCounts = np.zeros((len(intensityBins)-1,
                                            len(distanceBins)-1,
                                            len(areaBins)-1))

            else:
                for i in range(self.fragment_count()):
                    if not completeFragments[i] and decodeTask.is_complete(i):
                        barcodes = barcodeDB.get_barcodes(
                            i, columnList=['barcode_id', 'mean_intensity',
                                           'min_distance', 'area'])
                        blankCounts += self._extract_counts(
                            barcodes[barcodes['barcode_id'].isin(
                                codebook.get_blank_indexes())],
                            intensityBins, distanceBins, areaBins)
                        codingCounts += self._extract_counts(
                            barcodes[barcodes['barcode_id'].isin(
                                codebook.get_coding_indexes())],
                            intensityBins, distanceBins, areaBins)
                        updated = True
                        completeFragments[i] = True

                if updated:
                    self.dataSet.save_numpy_analysis_result(
                        blankCounts, 'blank_counts', self)
                    self.dataSet.save_numpy_analysis_result(
                        codingCounts, 'coding_counts', self)


class AdaptiveFilterBarcodes(analysistask.AnalysisTask):

    """
    An analysis task that filters barcodes based on a mean intensity threshold
    for each area based on the abundance of blank barcodes. The threshold
    is selected to achieve a specified misidentification rate.
    """

    def __init__(self, dataSet, parameters=None, analysisName=None):
        super().__init__(dataSet, parameters, analysisName)

        if 'misidentification_rate' not in self.parameters:
            self.parameters['misidentification_rate'] = 0.05

    def fragment_count(self):
        return len(self.dataSet.get_fovs())

    def get_barcode_database(self):
        return barcodedb.PyTablesBarcodeDB(self.dataSet, self)

    def get_estimated_memory(self):
        return 1000

    def get_estimated_time(self):
        return 30

    def get_dependencies(self):
        return [self.parameters['adaptive_task'],
                self.parameters['decode_task']]

    def get_adaptive_thresholds(self):
        return self.dataSet.load_numpy_analysis_result(
            'adaptive_thresholds', self)

    def get_codebook(self) -> Codebook:
        decodeTask = self.dataSet.load_analysis_task(
            self.parameters['decode_task'])
        return decodeTask.get_codebook()

    def _run_analysis(self):
        adaptiveTask = self.dataSet.load_analysis_task(
            self.parameters['adaptive_task'])
        decodeTask = self.dataSet.load_analysis_task(
            self.parameters['decode_task'])

        threshold = adaptiveTask.calculate_threshold_for_misidentification_rate(
            self.parameters['misidentification_rate'])

        bcDatabase = self.get_barcode_database()
        for f in self.dataSet.get_fovs():
            currentBarcodes = decodeTask.get_barcode_database().get_barcodes(f)
            bcDatabase.write_barcodes(
                adaptiveTask.extract_barcodes_with_threshold(
                    threshold, currentBarcodes), fov=f)
