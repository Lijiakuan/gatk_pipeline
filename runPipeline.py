# Copyright (C) 2013 DNAnexus, Inc.
#
# This file is part of gatk_pipeline (DNAnexus platform app).
#
# (The MIT Expat License)
#
#   Permission is hereby granted, free of charge, to any person obtaining a
#   copy of this software and associated documentation files (the "Software"),
#   to deal in the Software without restriction, including without limitation
#   the rights to use, copy, modify, merge, publish, distribute, sublicense,
#   and/or sell copies of the Software, and to permit persons to whom the
#   Software is furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
#   FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
#   DEALINGS IN THE SOFTWARE.

import dxpy
import subprocess, logging
import os
import sys
import re
import math
import operator
import time
import string

import dxpy
import subprocess, logging
import os, sys, re, math, operator, time
from multiprocessing import Pool, cpu_count

@dxpy.entry_point('main')
def main(**job_inputs):
    ## RUN DEDUPLICATE
    os.environ['CLASSPATH'] = '/opt/jar/MarkDuplicates.jar:/opt/jar/MergeSamFiles.jar:/opt/jar/AddOrReplaceReadGroups.jar:/opt/jar/GenomeAnalysisTK.jar'

    #Check that resources have the necessary requirements
    
    known = False
    training = False
    truth = False
    if 'gatk_resources' in job_inputs:
        for x in job_inputs['gatk_resources']:
            if x.get_details().get["known"] == True:
                known = True
            if x.get_details().get["training"] == True:
                training = True
            if x.get_details().get["truth"] == True:
                truth = True
        if not known or not training or not truth:
            raise dxpy.AppError("If any GATK recalibration resources are provided, at least one of each category: \"Known\", \"Training\", and \"Truth\" are required. Please either add an example of each, or do not provide any recalibation resources.")
        

    if 'output_name' in job_inputs:
        outputName = job_inputs['output_name']
    else:
        outputName = ''
    recalibratedTable = createNewMappingsTable(job_inputs['mappings'], outputName)
    #print "Mappings Table: " + mappingsTable.get_id()
    print "Recalibrated Table: " + recalibratedTable.get_id()

    mappingsTable = dxpy.DXGTable(job_inputs['mappings'][0]['$dnanexus_link'])
    for x in job_inputs['mappings']:
        if 'quality' not in dxpy.DXGTable(x).get_col_names():
            if len(job_inputs['mappings']) > 1:
                raise dxpy.AppError("One of the provided mappings did not have quality scores, for example %s. GATK can't recalibrate mappings without quality scores. You can try GATK UnifiedGenotyper to call variants without recalibration." % dxpy.DXGTable(x).describe()['name'])
            else:
                raise dxpy.AppError("The provided mappings did not have quality scores. GATK can't recalibrate mappings without quality scores. You can try GATK UnifiedGenotyper to call variants without recalibration.")

    try:
        contigSetId = mappingsTable.get_details()['original_contigset']['$dnanexus_link']
        originalContigSet = mappingsTable.get_details()['original_contigset']
    except:
        raise dxpy.AppError("The original reference genome must be attached as a detail")

    if contigSetId != job_inputs['reference']['$dnanexus_link']:
        raise dxpy.AppError("The reference genome of the mappings does not match the provided reference genome")

    samples = []
    for x in job_inputs['mappings']:
        samples.append(dxpy.DXGTable(x).describe()['name'].replace(" ", "_"))
    if job_inputs['call_multiple_samples']:
        samples = dxpy.DXGTable(job_inputs['mappings'][0]).describe()['name'].replace(" ", "_")
        
    gatkCommand = buildCommand(job_inputs)

    subprocess.check_call("dx-contigset-to-fasta %s ref.fa" % (job_inputs['reference']['$dnanexus_link']), shell=True)
    referenceFile = dxpy.upload_local_file("ref.fa", wait_on_close=True)


    reduceInput = {}
    bestPracticesJobs = []
    variantCallingCoordinatorInput = job_inputs
    variantCallingCoordinatorInput["recalibrated_bam"] = []
    variantCallingCoordinatorInput["mappings_tables"] = []
    variantCallingCoordinatorInput["reference_file"] = referenceFile.get_id()
    variantCallingCoordinatorInput["intervals_to_include"] = job_inputs["intervals_to_process"]
    variantCallingCoordinatorInput["intervals_to_exclude"] = job_inputs["intervals_to_exclude"]
    variantCallingCoordinatorInput["intervals_merging"] = job_inputs["intervals_merging"]
    mappingsImportCoordinatorInput = {"recalibrated_bam":[], 'recalibrated_table_id': recalibratedTable.get_id()}

    for x in job_inputs['mappings']:
        reads = int(dxpy.DXGTable(x).describe()['length'])
        chunks = int(reads/job_inputs['reads_per_job'])+1
        
        print "splits: " + str(chunks)

        #Split the genome into chunks to parallelize
        commandList = splitGenomeLengthChromosome(originalContigSet, chunks)

        
        for i in range(len(commandList)):
            if job_inputs['intervals_merging'] != "INTERSECTION" and "intervals_to_process" in job_inputs and job_inputs["intervals_to_process"] != "":
                print splitUserInputRegions(commandList[i], job_inputs['intervals_to_process'], "-L")
                commandList[i] = splitUserInputRegions(commandList[i], job_inputs['intervals_to_process'], "-L")
        print commandList
        commandList = [y for y in commandList if y != '']
        print commandList
        if len(commandList) == 0:
            raise dxpy.AppError("We could not find any overlap between the regions provided in \"Intervals to Process\" and the reference genome")
        
        chunks = len(commandList)
        excludeInterchromosome = (chunks > 1)
    

        for i in range(len(commandList)):
            mapBestPracticesInput = {
                'mappings_tables': mappingsTable.get_id(),
                'recalibrated_table_id': recalibratedTable.get_id(),
                'interval': commandList[i],
                'job_number' : i,
                'reference_file': referenceFile.get_id(),
                'dbsnp': job_inputs['dbsnp'],
                'separate_read_groups' : job_inputs['separate_read_groups'],
                'call_multiple_samples': job_inputs['call_multiple_samples'],
                'discard_duplicates': job_inputs['discard_duplicates'],
                'parent_input': job_inputs,
                'deduplicate_interchromosome': (job_inputs['deduplicate_interchromosome'] and chunks != 1),
                'gatk_command': gatkCommand,
                'compress_reference': job_inputs['compress_reference'],
                'infer_no_call': job_inputs['infer_no_call'],
                'compress_no_call': job_inputs['compress_no_call'],
                'intervals_to_include': job_inputs['intervals_to_process'],
                'intervals_to_exclude': job_inputs['intervals_to_exclude'],
                'intervals_merging': job_inputs['intervals_merging'],
            }
            if 'known_indels' in job_inputs:
                mapBestPracticesInput['known_indels'] = job_inputs['known_indels']
    
            mapJobId = dxpy.new_dxjob(fn_input=mapBestPracticesInput, fn_name="mapBestPractices").get_id()
            reduceInput["mapJob" + str(i)] = {'job': mapJobId, 'field': 'ok'}
            bestPracticesJobs.append(mapJobId)
            variantCallingCoordinatorInput["mappings_tables"].append(mappingsTable.get_id())
    for mapJobId in bestPracticesJobs:
        variantCallingCoordinatorInput["recalibrated_bam"].append({"job":mapJobId, "field": "recalibrated_bam"})
        mappingsImportCoordinatorInput["recalibrated_bam"].append({"job":mapJobId, "field": "recalibrated_bam"})
    variantCallingCoordinatorJob = dxpy.new_dxjob(fn_input=variantCallingCoordinatorInput, fn_name="variantCallingCoordinator").get_id()
    mappingsImportCoordinatorJob = dxpy.new_dxjob(fn_input=mappingsImportCoordinatorInput, fn_name="mappingsImportCoordinator").get_id()
    
    reduceInput["variants_table"] = {'job': variantCallingCoordinatorJob, 'field': 'variants_table'}
    reduceInput["gatk_completion"] = {'job': variantCallingCoordinatorJob, 'field': 'gatk_jobs'}
    reduceInput["import_jobs"] = {'job': mappingsImportCoordinatorJob, 'field': 'import_jobs'}
    reduceInput["recalibrated_table"] = recalibratedTable.get_id()

    
    if job_inputs.get("gatk_resources") != None:
        reduceInput["recalibrated_variants_table"] = {'job':variantCallingCoordinatorJob, 'field': "recalibrated_variants_table"}
        
    reduceJobId = dxpy.new_dxjob(fn_input=reduceInput, fn_name="reduceBestPractices").get_id()
    output = {'recalibrated_mappings': {'job': reduceJobId, 'field': 'recalibrated_table'}, 'variants': {'job': reduceJobId, 'field': 'variants_table'}}
    if job_inputs.get("gatk_resources") != None:
        output['recalibrated_variants'] = {'job': reduceJobId, 'field': 'recalibrated_variants_table'}

    return output

def runAndCatchGATKError(command, shell=True):
    # Added to capture any errors outputted by GATK
    try:
        subprocess.check_output(command, stderr=subprocess.STDOUT, shell=shell)
    except subprocess.CalledProcessError, e:
        print e 
        error = '\n'.join([l for l in e.output.splitlines() if l.startswith('##### ERROR MESSAGE:')])
        if error: 
            raise dxpy.AppError("App failed with GATK error. Please see logs for more information: {err}".format(err=error))
        else: 
            raise dxpy.AppInternalError("App failed with error. Please see logs for more information: {err}".format(err=e))         

@dxpy.entry_point('mappingsImportCoordinator')
def mappingsImportCoordinator(**job_inputs):
    
    output = {"import_jobs": []}
    for x in job_inputs['recalibrated_bam']:
        fileId = dxpy.DXFile(x).get_id()
    
        #Spin off Import Recalibrated Mappings job
        importRecalibratedMappingsInput = {
            'recalibrated_bam': fileId,
            'recalibrated_table_id': job_inputs['recalibrated_table_id']
        }
        output['import_jobs'].append({'job':dxpy.new_dxjob(fn_input=importRecalibratedMappingsInput, fn_name="importRecalibratedMappings").get_id(), 'field':'ok'})

    return output

@dxpy.entry_point('variantCallingCoordinator')
def variantCallingCoordinator(**job_inputs):
    os.environ['CLASSPATH'] = '/opt/jar/MarkDuplicates.jar:/opt/jar/SamFormatConverter.jar:/opt/jar/SortSam.jar:/opt/jar/AddOrReplaceReadGroups.jar:/opt/jar/MergeSamFiles.jar:/opt/jar/GenomeAnalysisTK.jar:/opt/jar/AddOrReplaceReadGroups.jar'
    
    mappingsTable = dxpy.DXGTable(job_inputs['mappings'][0]['$dnanexus_link'])
    originalContigSet = mappingsTable.get_details()['original_contigset']

    # Merge BAMs
    samples = []
    merges = {}
    for i in range(len(job_inputs['recalibrated_bam'])):
        if job_inputs['recalibrated_bam'][i] != '':
            name = dxpy.DXGTable(job_inputs['mappings_tables'][i]).describe()['name'].replace(" ", "_")
            if job_inputs['call_multiple_samples'] == False:
                name = "recalibrated"
            dxpy.download_dxfile(job_inputs['recalibrated_bam'][i], name+str(i)+".bam")
            if merges.get(name) == None:
                merges[name] = []
            merges[name].append(name+str(i)+".bam")
    
    commandInput = ''
    for k, v in merges.iteritems():
        samples.append(k)
        if len(v) == 1:
            subprocess.check_call(["mv", v[0], k + ".bam"])
        else:
            print "Merging Sam Files"
            print k + ".bam"
            commandInput += " -I %s.bam" % k
            mergeSamCommand = "java -Xmx6g net.sf.picard.sam.MergeSamFiles OUTPUT=%s.bam USE_THREADING=true SORT_ORDER=coordinate VALIDATION_STRINGENCY=SILENT" % k
            for x in v:
                mergeSamCommand += " INPUT=" + x
            print "Merge Command"
            print mergeSamCommand
            runAndCatchGATKError(mergeSamCommand, shell=True)

        
    variantsTable = buildVariantsTable(job_inputs, mappingsTable, samples, dxpy.DXRecord(originalContigSet).get_id(), '')
    recalibratedVariantsTable = buildVariantsTable(job_inputs, mappingsTable, samples, dxpy.DXRecord(originalContigSet).get_id(), ' Recalibrated')
    
    gatkCommand = buildCommand(job_inputs)
 
    reads = 0
    for x in job_inputs['mappings']:
        mappingsTable = dxpy.DXGTable(x)
        reads += int(mappingsTable.describe()['length'])
    
    chunks = int(reads/job_inputs['reads_per_job'])+1    
    #Split the genome into chunks to parallelize
    commandList = splitGenomeLengthChromosome(originalContigSet, chunks)
    for i in range(len(commandList)):
            if job_inputs['intervals_merging'] != "INTERSECTION" and "intervals_to_include" in job_inputs and job_inputs["intervals_to_include"] != "":
                commandList[i] = splitUserInputRegions(commandList[i], job_inputs['intervals_to_include'], "-L")
    commandList = [y for y in commandList if y != '']
                
    chunks = len(commandList)
    
    gatkJobs = []
    for i in range(len(commandList)):
        inputFiles = []
        for j in range(len(samples)):
            
            inputFiles.append(dxpy.upload_local_file("%s.bam" % samples[j], wait_on_close=True).get_id())
            
        mapGatkInput = {
            'mappings_files': inputFiles,
            'reference_file': job_inputs['reference_file'],
            'interval': commandList[i],
            'tableId': variantsTable.get_id(),
            'command': gatkCommand,
            'compress_reference': job_inputs['compress_reference'],
            'infer_no_call': job_inputs['infer_no_call'],
            'compress_no_call': job_inputs['compress_no_call'],
            'intervals_to_include': job_inputs['intervals_to_process'],
            'intervals_to_exclude': job_inputs['intervals_to_exclude'],
            'intervals_merging': job_inputs['intervals_merging'],
            'part_number': i
        }
        # Run a "map" job for each chunk
        gatkJobs.append(dxpy.new_dxjob(fn_input=mapGatkInput, fn_name="mapGatk").get_id())

    output = {}
    if job_inputs.get('gatk_resources') != None:
        variantRecalibrationInput = job_inputs
        variantRecalibrationInput["vcfs"] = []
        variantRecalibrationInput["recalibrated_variants_table"] = recalibratedVariantsTable.get_id()
        for x in gatkJobs:
            variantRecalibrationInput["vcfs"].append({"job":x, "field":"file_id"})
        output['variant_recalibration_job'] = {'job': dxpy.new_dxjob(fn_input=variantRecalibrationInput, fn_name="recalibrateVariants").get_id(), 'field':'ok'}
        output['recalibrated_variants_table'] = recalibratedVariantsTable.get_id()
        
    output['gatk_jobs'] = []
    for x in gatkJobs:
        output['gatk_jobs'].append({'job':x, 'field':'ok'})
    output['variants_table'] = variantsTable.get_id()

    return output

@dxpy.entry_point('reduceBestPractices')
def reduceBestPractices(**job_inputs):
    startTime = time.time()
    recalibratedTable = dxpy.DXGTable(job_inputs['recalibrated_table'])
    recalibratedTable.close()
    print "Table closing completed in " + str(int((time.time()-startTime)/60)) + " minutes"
    variantsTable = dxpy.DXGTable(job_inputs['variants_table'])
    variantsTable.close()

    output = {}
    if job_inputs.get("recalibrated_variants_table") != None and job_inputs["recalibrated_variants_table"] != '':
        recalibratedVariantsTable = dxpy.DXGTable(job_inputs["recalibrated_variants_table"])
        recalibratedVariantsTable.close()
        output['recalibrated_variants_table'] = dxpy.dxlink(recalibratedVariantsTable.get_id())
    
    output['recalibrated_table'] = dxpy.dxlink(recalibratedTable.get_id())
    output['variants_table'] = dxpy.dxlink(variantsTable.get_id())

    return output

def writeUnmappedReads(mappingsTable, dedupTable):
    colNames = dedupTable.get_col_names()
    col = {}
    for i in range(len(colNames)):
        col[colNames[i]] = i
    for row in mappingsTable.iterate_rows():
        entry = []
        if row[col["chr"]] != '':
            break
        if row[col["chr"]] == row[col["chr2"]]:
            dedupTable.add_rows([entry])


def buildVariantsTable(job_inputs, mappingsTable, samples, reference_id, appendToName):
    if job_inputs['output_mode'] == "EMIT_VARIANTS_ONLY":
        job_inputs['infer_no_call'] = False

    variants_schema = [
        {"name": "chr", "type": "string"},
        {"name": "lo", "type": "int32"},
        {"name": "hi", "type": "int32"},
        {"name": "ref", "type": "string"},
        {"name": "alt", "type": "string"},
        {"name": "qual", "type": "double"},
        {"name": "ids", "type": "string"}
         ]

    elevatedTags = ['format_GT', 'format_DP', 'format_AD']
    headerInfo = extractHeader("/tmp/header.txt", elevatedTags)
    description = {}

    indices = [dxpy.DXGTable.genomic_range_index("chr","lo","hi", 'gri')]

    formats = {}
    infos = {}
    filters = {}

    for k, v in headerInfo['tags']['info'].iteritems():
        variants_schema.append({"name": "info_"+k, "type":translateTagTypeToColumnType(v)})
        description[k] = {'name' : k, 'description' : v['description'], 'type' : v['type'], 'number' : v['number']}

    #For each sample, write the sample-specific columns
    for i in range(len(samples)):
      variants_schema.extend([
        {"name": "genotype_"+str(i), "type": "string"},
        {"name": "phasing_"+str(i), "type": "string"},
        {"name": "type_"+str(i), "type": "string"},
        {"name": "variation_qual_"+str(i), "type": "double"},
        {"name": "genotype_qual_"+str(i), "type": "double"},
        {"name": "coverage_"+str(i), "type": "string"},
        {"name": "total_coverage_"+str(i), "type": "int32"}
      ])
      for k, v in headerInfo['tags']['format'].iteritems():
        if "format_"+k not in elevatedTags:
          variants_schema.append({"name": "format_"+k+"_"+str(i), "type":translateTagTypeToColumnType(v)})

    variantsTable = dxpy.new_dxgtable(variants_schema, indices=[dxpy.DXGTable.genomic_range_index("chr", "lo", "hi", "gri")])
    tableId = variantsTable.get_id()
    variantsTable = dxpy.open_dxgtable(tableId)
    variantsTable.add_types(["Variants", "gri"])

    details = {'samples':samples, 'original_contigset':dxpy.dxlink(reference_id), 'formats':headerInfo['tags']['format'], 'infos':headerInfo['tags']['info']}
    #if headerInfo.get('filters') != {}:
    #  details['filters'] = headerInfo['filters']
    variantsTable.set_details(details)

    if 'output_name' in job_inputs:
        variantsTable.rename(job_inputs['output_name'] + appendToName)
    elif (job_inputs['genotype_likelihood_model'] == "SNP"):
        variantsTable.rename(mappingsTable.describe()['name'] + " SNP calls by GATK" + appendToName)
    elif (job_inputs['genotype_likelihood_model'] == "INDEL"):
        variantsTable.rename(mappingsTable.describe()['name'] + " indel calls by GATK" + appendToName)
    elif (job_inputs['genotype_likelihood_model'] == "BOTH"):
        variantsTable.rename(mappingsTable.describe()['name'] + " SNP and indel calls by GATK" + appendToName)
    else:
        variantsTable.rename(mappingsTable.describe()['name'] + " variant calls by GATK" + appendToName)

    return variantsTable

@dxpy.entry_point('importRecalibratedMappings')
def importRecalibratedMappings(**job_inputs):
    recalibratedTable = dxpy.DXGTable(job_inputs['recalibrated_table_id'])
    
    dxpy.DXFile(job_inputs['recalibrated_bam']).wait_on_close()
    
    dxpy.download_dxfile(job_inputs['recalibrated_bam'], "recalibrated.bam")
    subprocess.check_call("samtools view -h -o recalibrated.sam recalibrated.bam", shell=True)

    default = {}
    recalibratedColNames = recalibratedTable.get_col_names()
    recalibratedCol = {}
    for i in range(len(recalibratedColNames)):
        recalibratedCol[recalibratedColNames[i]] = i

    for x in recalibratedTable.describe()['columns']:
        if "int" in x["type"]:
            default[x["name"]] = sys.maxint
        elif x["type"] == "float":
            default[x["name"]] = float(sys.maxint)
        elif x["type"] == "boolean":
            default[x["name"]] = False
        else:
            default[x["name"]] = ""

    print "Writing mate pair information for lookup"
    startTime = time.time()
    if recalibratedCol.get("chr2") != None:
        mateLocations = {}
        for line in open("recalibrated.sam", 'r'):
            try:
                if line[0] != "@":
                    tabSplit = line.split("\t")
                    if len(tabSplit) > 9:
                        chr = tabSplit[2]
                        lo = int(tabSplit[3])-1
                        templateId = int(tabSplit[0])
                        cigar = re.split('(\d+)', tabSplit[5])
                        alignLength = 0
                        for p in range(len(cigar)):
                            c = cigar[p]
                            if c == 'M' or c == 'D' or c == 'N' or c == 'X' or c == 'P' or c == '=':
                                alignLength += int(cigar[p-1])
                        hi = lo + alignLength
        
                        recalibrationTags = re.findall("zd:Z:([^\t]*)[\t\n]", line)[0].split("##&##")
                        reportedLo = int(recalibrationTags[1])
                        reportedHi = int(recalibrationTags[2])
                        
                        if lo != reportedLo or hi != reportedHi:
                            if int(tabSplit[1]) & 0x1 & 0x40:
                                mateLocations[templateId] = {0: {"lo":lo, "hi":hi, "chr":chr}}
                            elif int(tabSplit[1]) & 0x1 & 0x80:
                                mateLocations[templateId] = {1: {"lo":lo, "hi":hi, "chr":chr}}
            except:
                print line
                raise dxpy.AppError("The resulting BAM may have had a trucated line. This may be the result of a random error and it's possible a retry of the same job will work.")
        print str(len(mateLocations)) + " Interchromosomal reads changed lo or hi"
    print "Interchromosome changes to lo and hi recorded in " + str(int((time.time()-startTime)/60)) + " minutes"

    complement_table = string.maketrans("ATGCatgc", "TACGtacg")
    rowsWritten = 0

    startTime = time.time()
    for line in open("recalibrated.sam", 'r'):
        try:
            if line[0] != "@":
                tabSplit = line.split("\t")
                if len(tabSplit) > 9:
                    templateId = int(tabSplit[0])
                    flag = int(tabSplit[1])
                    chr = tabSplit[2]
                    lo = int(tabSplit[3])-1
                    qual = tabSplit[10]
                    alignLength = 0
                    mapq = int(tabSplit[4])
                    cigar = re.split('(\d+)', tabSplit[5])
                    duplicate = (flag & 0x400 == True)
                    sequence = tabSplit[9]
                    recalibrationTags = re.findall("zd:Z:([^\t]*)[\t\n]", line)[0].split("##&##")
        
                    name = recalibrationTags[0]
                    readGroup = int(re.findall("RG:Z:(\d+)", line)[0])
        
                    if flag & 0x4:
                        status = "UNMAPPED"
                    elif flag & 0x100:
                        status = "SECONDARY"
                    else:
                        status = "PRIMARY"
                    if flag & 0x200:
                        qcFail = True
                    else:
                        qcFail = False
                    if flag & 0x10 == 0 or status == "UNMAPPED":
                        negativeStrand = False
                    else:
                        negativeStrand = True
                        sequence = sequence.translate(complement_table)[::-1]
                        qual = qual[::-1]
        
                    for p in range(len(cigar)):
                        c = cigar[p]
                        if c == 'M' or c == 'D' or c == 'N' or c == 'X' or c == 'P' or c == '=':
                            alignLength += int(cigar[p-1])
                    cigar = tabSplit[5]
                    hi = lo + alignLength
        
                    if recalibratedCol.get("chr2") != None:
                        properPair=False
                        if (flag & 0x1) and (flag & 0x2):
                            properPair = True
                        try:
                            reportedChr2 = recalibrationTags[3]
                            reportedLo2 = int(recalibrationTags[4])
                            reportedHi2 = int(recalibrationTags[5])
                            status2 = recalibrationTags[6]
                        except:
                            print line
                            raise dxpy.AppError("The resulting BAM may have had a trucated line. This may be the result of a random error and it's possible a retry of the same job will work.")
        
                        if not flag & 0x1:
                            negativeStrand2 = False
                        elif flag & 0x20 and status2 != "UNMAPPED":
                            negativeStrand2 = True
                        else:
                            negativeStrand2 = False
        
                        if flag & 0x1:
                            if flag & 0x40:
                                mateId = 0
                            elif flag & 0x80:
                                mateId = 1
                        else:
                            mateId = -1
        
                        try:
                            if mateId == 1:
                                chr2 = mateLocations[tabSplit[0]][0]["chr2"]
                                lo2 = mateLocations[tabSplit[0]][0]["lo"]
                                hi2 = mateLocations[tabSplit[0]][0]["hi"]
                            elif mateId == 0:
                                chr2 = mateLocations[tabSplit[0]][1]["chr2"]
                                lo2 = mateLocations[tabSplit[0]][1]["lo"]
                                hi2 = mateLocations[tabSplit[0]][1]["hi"]
                            else:
                                chr2 = reportedChr2
                                lo2 = reportedLo2
                                hi2 = reportedHi2
                        except:
                            chr2 = reportedChr2
                            lo2 = reportedLo2
                            hi2 = reportedHi2
                        recalibratedTable.add_rows([[sequence, name, qual, status, chr, lo, hi, negativeStrand, mapq, qcFail, duplicate, cigar, templateId, readGroup, mateId, status2, chr2, lo2, hi2, negativeStrand2, properPair]])
                    else:
                        recalibratedTable.add_rows([[sequence, name, qual, status, chr, lo, hi, negativeStrand, mapq, qcFail, duplicate, cigar, templateId, readGroup]])
                    rowsWritten += 1
                    if rowsWritten%100000 == 0:
                        print "Imported " + str(rowsWritten) + " rows. Time taken: " + str(int((time.time()-startTime)/60)) + " minutes"
                        recalibratedTable.flush()
        except:
            print line
            raise dxpy.AppError("The resulting BAM may have had a trucated line. This may be the result of a random error and it's possible a retry of the same job will work.")
    recalibratedTable.flush()
    output = {'ok': True}

    return output

@dxpy.entry_point('mapBestPractices')
def mapBestPractices(**job_inputs):
    os.environ['CLASSPATH'] = '/opt/jar/MarkDuplicates.jar:/opt/jar/SamFormatConverter.jar:/opt/jar/SortSam.jar:/opt/jar/AddOrReplaceReadGroups.jar:/opt/jar/MergeSamFiles.jar:/opt/jar/GenomeAnalysisTK.jar:/opt/jar/AddOrReplaceReadGroups.jar'

    jobNumber = job_inputs['job_number']

    regionFile = open("regions.txt", 'w')
    print job_inputs['interval']
    regionFile.write(job_inputs['interval'])
    regionFile.close()

    readGroups = 0
    print "Converting Table to SAM"
    mappingsTable = job_inputs['mappings_tables']
    
    sampleName = "recalibrated"
    if job_inputs['call_multiple_samples']:
        sampleName = dxpy.DXGTable(mappingsTable).describe()['name'].replace(" ", "_")
    
    command = "pypy /usr/bin/dx_mappings_to_sam3 %s --output input.sam --region_index_offset -1 --id_as_name --region_file regions.txt --write_row_id --read_group_platform illumina --sample %s" % (mappingsTable, sampleName)
    if job_inputs['deduplicate_interchromosome']:
        command += " --distribute_interchromosomal"
    print command
    startTime = time.time()
    subprocess.check_call(command, shell=True)
    print "Download mappings completed in " + str(int((time.time()-startTime)/60)) + " minutes"

    readsPresent = False

    output = {}
    if checkSamContainsRead("input.sam"):
        startTime = time.time()
        subprocess.check_call("samtools view -bS input.sam > input.bam", shell=True)
        subprocess.check_call("samtools sort input.bam input.sorted", shell=True)
        subprocess.check_call("mv input.sorted.bam input.bam", shell=True)
        runAndCatchGATKError("java -Xmx4g net.sf.picard.sam.MarkDuplicates I=input.bam O=dedup.bam METRICS_FILE=metrics.txt ASSUME_SORTED=true VALIDATION_STRINGENCY=SILENT REMOVE_DUPLICATES=%s" % job_inputs["discard_duplicates"])        
        print "Mark Duplicates completed in: " + str(int((time.time()-startTime)/60)) + " minutes"
    else:
        #Just take the header since that will allow merge with an empty file
        subprocess.check_call("samtools view -HbS input.sam > recalibrated.bam", shell=True)
        result = dxpy.upload_local_file("recalibrated.bam", wait_on_close=True)
        output['recalibrated_bam'] = result.get_id()
        output['import_job'] = ''
        output['ok'] = True
        return output

    dxpy.DXFile(job_inputs['reference_file']).wait_on_close()
    dxpy.download_dxfile(job_inputs['reference_file'], "ref.fa")

    
    subprocess.check_call("samtools index dedup.bam", shell=True)

    #RealignerTargetCreator
    command = "java -Xmx4g org.broadinstitute.sting.gatk.CommandLineGATK -T RealignerTargetCreator -R ref.fa -I dedup.bam -o indels.intervals -rf BadCigar"
    command += job_inputs['interval']

    #Download the known indels
    knownCommand = ''
    if 'known_indels' in job_inputs:
        for i in range(len(job_inputs['known_indels'])):
            dxpy.download_dxfile(job_inputs['known_indels'][i], "indels"+str(i)+".vcf.gz")
            knownFileName = "indels"+str(i)+".vcf.gz"
            try:
                p = subprocess.Popen("tabix -f -p vcf " + knownFileName, stderr=subprocess.PIPE, shell=True)
                if '[tabix] was bgzip' in p.communicate()[1]:
                    subprocess.check_call("zcat -f " + knownFileName + " | bgzip -c >temp.vcf.gz && mv -f temp.vcf.gz " + knownFileName + " && tabix -p vcf " + knownFileName, shell=True)
            except subprocess.CalledProcessError:
                raise dxpy.AppError("An error occurred while trying to index the provided known indels with tabix. Please make sure the provided known indels are valid VCF files.")
            knownCommand += " -known " + knownFileName
        command += knownCommand

    #Find chromosomes
    regionChromosomes = []
    for x in re.findall("-L ([^:]*):\d+-\d+", job_inputs['interval']):
        regionChromosomes.append(x)

    #Add options for RealignerTargetCreator
    if job_inputs['parent_input']['window_size'] != 10:
        command += "--windowSize " + str(job_inputs['parent_input']['window_size'])
    if job_inputs['parent_input']['max_interval_size'] != 500:
        command += " --maxIntervalSize "  + str(job_inputs['parent_input']['max_interval_size'])
    if job_inputs['parent_input']['min_reads_locus'] != 4:
        command += " --minReadsAtLocus " + str(job_inputs['parent_input']['min_reads_locus'])
    if job_inputs['parent_input']['mismatch_fraction'] != 0.0:
        command += " --mismatchFraction " + str(job_inputs['parent_input']['mismatch_fraction'])

    print command
    runAndCatchGATKError(command, shell=True)

    #Run the IndelRealigner
    command = "java -Xmx4g org.broadinstitute.sting.gatk.CommandLineGATK -T IndelRealigner -R ref.fa -I dedup.bam -targetIntervals indels.intervals -o realigned.bam -rf BadCigar"
    command += job_inputs['interval']
    command += knownCommand
    if "consensus_model" in job_inputs['parent_input']:
        if job_inputs['parent_input']['consensus_model'] != "":
            if job_inputs['parent_input']['consensus_model'] == "USE_READS" or job_inputs['parent_input']['consensus_model'] == "KNOWNS_ONLY" or job_inputs['parent_input']['consensus_model'] == "USE_SW":
                command += " --consensusDeterminationModel " + job_inputs['parent_input']['consensus_model']
            else:
                raise dxpy.AppError("The option \"Consensus Determination Model\" must be either blank or one of [\"USE_READS\", \"KNWONS_ONLY\", or \"USE_SW\"], found " + job_inputs['parent_input']['consensus_model'] + " instead.")
    if job_inputs['parent_input']['lod_threshold'] != 5.0:
        command += " --LODThresholdForCleaning " + str(job_inputs['parent_input']['lod_threshold'])
    if job_inputs['parent_input']['entropy_threshold'] != 0.15:
        command += " --entropyThreshold " + str(job_inputs['parent_input']['entropy_threshold'])
    if job_inputs['parent_input']['max_consensuses'] != 30:
        command += " --maxConsensuses " + str(job_inputs['parent_input']['max_consensuses'])
    if job_inputs['parent_input']['max_insert_size_movement'] != 3000:
        command += " --maxIsizeForMovement " + str(job_inputs['parent_input']['max_insert_size_movement'])
    if job_inputs['parent_input']['max_position_move'] != 200:
        command += " --maxPositionalMoveAllowed " + str(job_inputs['parent_input']['max_position_move'])
    if job_inputs['parent_input']['max_reads_consensus'] != 120:
        command += " --maxReadsForConsensus " + str(job_inputs['parent_input']['maxReadsForRealignment'])
    if job_inputs['parent_input']['max_reads_realignment'] != 20000:
        command += " --maxReadsForRealignment " + str(job_inputs['parent_input']['max_reads_realignment'])

    print command
    runAndCatchGATKError(command, shell=True)

    # Download dbsnp
    startTime = time.time()
    dxpy.download_dxfile(job_inputs['dbsnp'], "dbsnp.vcf.gz")
    print "Download dbsnp completed in " + str(int((time.time()-startTime)/60)) + " minutes"

    dbsnpFileName = 'dbsnp.vcf.gz'
    try:
        p = subprocess.Popen("tabix -p vcf dbsnp.vcf.gz", stderr=subprocess.PIPE, shell=True)
        if '[tabix] was bgzip' in p.communicate()[1]:
            subprocess.check_call("zcat -f dbsnp.vcf.gz | bgzip -c >temp.vcf.gz && mv -f temp.vcf.gz dbsnp.vcf.gz && tabix -p vcf dbsnp.vcf.gz", shell=True)
    except subprocess.CalledProcessError:
        raise dxpy.AppError("An error occurred while trying to index the provided dbSNP file with tabix. Please make sure the provided dbSNP file is a valid VCF file.")

    #Count Covariates
    command = "java -Xmx4g org.broadinstitute.sting.gatk.CommandLineGATK -T CountCovariates -R ref.fa -recalFile recalibration.csv -I realigned.bam -cov ReadGroupCovariate -cov QualityScoreCovariate -cov CycleCovariate -cov DinucCovariate --standard_covs -rf BadCigar"
    command += " -knownSites " + dbsnpFileName
    command += job_inputs['interval']
    if job_inputs['parent_input'].get('single_threaded') != True:
      command += " --num_threads " + str(cpu_count())
    command += " --solid_recal_mode " + job_inputs['parent_input']['solid_recalibration_mode']
    command += " --solid_nocall_strategy " + job_inputs['parent_input']['solid_nocall_strategy']
    if 'context_size' in job_inputs['parent_input']:
        command += " --context_size " + str(job_inputs['parent_input']['context_size'])
    if 'nback' in job_inputs['parent_input']:
        command += " --homopolymer_nback " + str(job_inputs['parent_input']['nback'])
    if job_inputs['parent_input']['cycle_covariate']:
        command += " -cov CycleCovariate"
    if job_inputs['parent_input']['dinuc_covariate']:
        command += " -cov DinucCovariate"
    if job_inputs['parent_input']['primer_round_covariate']:
        command += " -cov PrimerRoundCovariate"
    if job_inputs['parent_input']['mapping_quality_covariate']:
        command += " -cov MappingQualityCovariate"
    if job_inputs['parent_input']['homopolymer_covariate']:
        command += " -cov HomopolymerCovariate"
    if job_inputs['parent_input']['gc_content_covariate']:
        command += " -cov GCContentCovariate"
    if job_inputs['parent_input']['position_covariate']:
        command += " -cov PositionCovariate"
    if job_inputs['parent_input']['minimum_nqs_covariate']:
        command += " -cov MinimumNQSCovariate"
    if job_inputs['parent_input']['context_covariate']:
        command += " -cov ContextCovariate"

    print command
    runAndCatchGATKError(command, shell=True)

    #Table Recalibration
    command = "java -Xmx4g org.broadinstitute.sting.gatk.CommandLineGATK -T TableRecalibration -R ref.fa -recalFile recalibration.csv -I realigned.bam -o recalibrated.bam --doNotWriteOriginalQuals -rf BadCigar"
    command += job_inputs['interval']
    if "solid_recalibration_mode" in job_inputs['parent_input']:
        if job_inputs['parent_input']['solid_recalibration_mode'] != "":
            command += " --solid_recal_mode " + job_inputs['parent_input']['solid_recalibration_mode']
    if "solid_nocall_strategy" in job_inputs['parent_input']:
        if job_inputs['parent_input']['solid_nocall_strategy'] != "":
            command += " --solid_nocall_strategy " + job_inputs['parent_input']['solid_nocall_strategy']
    if 'context_size' in job_inputs['parent_input']:
        command += " --context_size " + str(job_inputs['parent_input']['context_size'])
    if 'nback' in job_inputs['parent_input']:
        command += " --homopolymer_nback " + str(job_inputs['parent_input']['nback'])
    if job_inputs['parent_input']['preserve_qscore'] != 5:
        command += " --preserve_qscores_less_than " + str(job_inputs['parent_input']['preserve_qscore'])
    if 'smoothing' in job_inputs['parent_input']:
        command += " --smoothing " + str(job_inputs['parent_input']['smoothing'])
    if 'max_quality' in job_inputs['parent_input']:
        command += " --max_quality_score " + str(job_inputs['parent_input']['max_quality'])
    print command
    runAndCatchGATKError(command, shell=True)

    result = dxpy.upload_local_file("recalibrated.bam", wait_on_close=True)
    print "Recalibrated file: " + result.get_id()
    
    output['recalibrated_bam'] = result.get_id()
    output['ok'] = True

    return output

def splitGenomeLengthChromosome(contig_set, chunks):
    details = dxpy.DXRecord(contig_set).get_details()
    sizes = details['contigs']['sizes']
    names = details['contigs']['names']
    offsets = details['contigs']['offsets']

    commandList = []
    chunkSizes = []

    totalSize = sum(sizes)
    readsPerChunk = int(float(totalSize)/chunks)

    for i in range(chunks):
        commandList.append('')
        chunkSizes.append(0)

    chromosome = 0
    position = 0
    totalSizes = []
    print chunkSizes
    for x in range(chunks):
        totalSizes.append(0)
    while chromosome < len(names):
        try:
            minimum = min([x for x in chunkSizes if x > 0])
            position = chunkSizes.index(minimum)
        except:
            position = 0
        if chunkSizes[position] + sizes[chromosome] > readsPerChunk:
            try:
                position = chunkSizes.index(0)
            except:
                position = chunkSizes.index(min([x for x in chunkSizes if x > 0]))
        commandList[position] += " -L %s:%d-%d" % (names[chromosome], 1, sizes[chromosome])
        chunkSizes[position] += sizes[chromosome]
        totalSizes[position] += sizes[chromosome]
        chromosome += 1
    commandList = filter(None, commandList)
    return commandList

def checkIntervalRange(includeList, chromosome, lo, hi):
    included = False
    command = ''
    if len(includeList) == 0:
        return " -L %s:%d-%d" % (chromosome, lo, hi)
    if includeList.get(chromosome) != None:
        for x in includeList[chromosome]:
            min = lo
            max = hi
            if (lo >= x[0] and lo <= x[1]) or (hi <= x[1] and hi >= x[0]):
                if lo >= x[0] and lo <= x[1]:
                    min = lo
                elif lo <= x[0]:
                    min = x[0]
                if hi <= x[1] and hi >= x[0]:
                    max = hi
                elif hi >= x[1]:
                    max = x[1]
                command += " -L %s:%d-%d" % (chromosome, min, max)
    return command

def checkSamContainsRead(samFileName):
    for line in open(samFileName, 'r'):
        if line[0] != "@":
            return True
    return False


def createNewMappingsTable(mappingsArray, recalibratedName):

    columns = []
    tags = []
    indices = []
    types = []
    read_groups = []
    for i in range(len(mappingsArray)):
        oldTable = dxpy.DXGTable(mappingsArray[i]['$dnanexus_link'])
        if oldTable.get_details()['read_groups'] != None:
            read_groups.extend(oldTable.get_details()['read_groups'])
        for x in oldTable.describe()['columns']:
            if x not in columns:
                columns.append(x)
        for x in oldTable.describe()['indices']:
            if x not in indices:
                indices.append(x)
        for x in oldTable.describe()['tags']:
            if x not in tags:
                tags.append(x)
        for x in oldTable.describe()['types']:
            if x not in types:
                types.append(x)

    schema = [{"name": "sequence", "type": "string"}]
    schema.append({"name": "name", "type": "string"})
    schema.append({"name": "quality", "type": "string"})
    schema.extend([{"name": "status", "type": "string"},
                          {"name": "chr", "type": "string"},
                          {"name": "lo", "type": "int32"},
                          {"name": "hi", "type": "int32"},
                          {"name": "negative_strand", "type": "boolean"},
                          {"name": "error_probability", "type": "uint8"},
                          {"name": "qc_fail", "type": "boolean"},
                          {"name": "duplicate", "type": "boolean"},
                          {"name": "cigar", "type": "string"},
                          {"name": "template_id", "type": "int64"},
                          {"name": "read_group", "type": "int32"}])
    if {"type":"string", "name":"chr2"} in columns:
        schema.extend([{"name": "mate_id", "type": "int32"}, # TODO: int8
                              {"name": "status2", "type": "string"},
                              {"name": "chr2", "type": "string"},
                              {"name": "lo2", "type": "int32"},
                              {"name": "hi2", "type": "int32"},
                              {"name": "negative_strand2", "type": "boolean"},
                              {"name": "proper_pair", "type": "boolean"}])

    oldTable = dxpy.DXGTable(mappingsArray[0]['$dnanexus_link'])
    if recalibratedName == '':
        recalibratedName = oldTable.describe()['name'] + " Realigned and Recalibrated"
    details = oldTable.get_details()
    details['read_groups'] = read_groups
    newTable = dxpy.new_dxgtable(columns=schema, indices=indices)
    newTable.add_tags(tags)
    newTable.set_details(details)
    newTable.add_types(types)
    newTable.rename(recalibratedName)

    return newTable

def buildCommand(job_inputs):

    command = "java -Xmx4g org.broadinstitute.sting.gatk.CommandLineGATK -T UnifiedGenotyper -R ref.fa -o output.vcf -rf BadCigar"
    if job_inputs['output_mode'] != "EMIT_VARIANTS_ONLY":
        command += " -out_mode " + (job_inputs['output_mode'])
    if job_inputs['call_confidence'] != 30.0:
        command += " -stand_call_conf " +str(job_inputs['call_confidence'])
    if job_inputs['emit_confidence'] != 30.0:
        command += " -stand_emit_conf " +str(job_inputs['emit_confidence'])
    if job_inputs['intervals_merging'] == "INTERSECTION":
        if job_inputs.get('intervals_to_process') != None:
            command += " " + job_inputs['intervals_to_process']
    if job_inputs.get('intervals_to_exclude') != None:
        command += " " + job_inputs['intervals_to_exclude']
    if job_inputs['pcr_error_rate'] != 0.0001:
        command += " -pcr_error " +str(job_inputs['pcr_error_rate'])
    if job_inputs['heterozygosity'] != 0.001:
        command += " -hets " + str(job_inputs['heterozygosity'])
    if job_inputs['indel_heterozygosity'] != 0.000125:
        command += " -indelHeterozygosity " + str(job_inputs['indel_heterozygosity'])
    if job_inputs['genotype_likelihood_model'] != "SNP":
        command += " -glm " + job_inputs['genotype_likelihood_model']
    if job_inputs['minimum_base_quality'] != 17:
        command += " -mbq " + str(job_inputs['minimum_base_quality'])
    if job_inputs['max_alternate_alleles'] != 3:
        command += " -maxAlleles " + str(job_inputs['max_alternate_alleles'])
    if job_inputs['max_deletion_fraction'] != 0.05:
        command += " -deletions " + str(job_inputs['max_deletion_fraction'])
    if job_inputs['min_indel_count'] != 5:
        command += " -minIndelCnt " + str(job_inputs['min_indel_count'])
    if job_inputs['non_reference_probability_model'] != "EXACT":
        if job_inputs['non_reference_probability_model'] != "GRID_SEARCH":
            raise dxpy.AppError("Option \"Probability Model\" must be either \"EXACT\" or \"GRID_SEARCH\". Found " + job_inputs['non_reference_probability_model'] + " instead")
        command += " -pnrm " + str(job_inputs['non_reference_probability_model'])

    if job_inputs.get('single_threaded') != True:
        command += " --num_threads " + str(cpu_count())
    command += " -L regions.interval_list"

    if job_inputs['downsample_to_coverage'] != 250:
        command += " -dcov " + str(job_inputs['downsample_to_coverage'])
    elif job_inputs['downsample_to_fraction'] != 1.0:
        command += " -dfrac " + str(job_inputs['downsample_to_fraction'])

    if job_inputs['nondeterministic']:
        command += " -ndrs "

    if job_inputs['calculate_BAQ'] != "OFF":
        if job_inputs['calculate_BAQ'] != "CALCULATE_AS_NECESSARY" and job_inputs['calculate_BAQ'] != "RECALCULATE":
            raise dxpy.AppError("Option \"Calculate BAQ\" must be either \"OFF\" or or \"CALCULATE_AS_NECESSARY\" \"RECALCULATE\". Found " + job_inputs['calculate_BAQ'] + " instead")
        command += " -baq " + job_inputs['calculate_BAQ']
        if job_inputs['BAQ_gap_open_penalty'] != 40.0:
            command += " -baqGOP " + str(job_inputs['BAQ_gap_open_penalty'])
    if job_inputs['no_output_SLOD']:
        command += " -nosl"

    return command

def extractHeader(vcfFileName, elevatedTags):
    result = {'columns': '', 'tags' : {'format' : {}, 'info' : {} }, 'filters' : {}}
    for line in open(vcfFileName):
        tag = re.findall("ID=(\w+),", line)
        if len(tag) > 0:
          tagType = ''
          if line.count("FORMAT") > 0:
            tagType = 'format'
          elif line.count("INFO") > 0:
            tagType = 'info'
          elif line.count("FILTER") > 0:
            result['filters'][re.findall("ID=(\w+),")[0]] = re.findall('Description="(.*)"')[0]

          typ = re.findall("Type=(\w+),", line)
          if tagType != '':
            number = re.findall("Number=(\w+)", line)
            description = re.findall('Description="(.*)"', line)
            if len(number) == 0:
              number = ['.']
            if len(description) == 0:
              description = ['']
            if "format_"+tag[0] not in elevatedTags:
                result['tags'][tagType][tag[0]] = {'type':typ[0], 'description' : description[0], 'number' : number[0]}
        if line[0] == "#" and line[1] != "#":
          result['columns'] = line.strip()
        if line == '' or line[0] != "#":
            break
    return result

def checkSamContainsRead(samFileName):
    for line in open(samFileName, 'r'):
        if line[0] != "@":
            return True
    return False

def translateTagTypeToColumnType(tag):
  if tag['type'] == "Flag":
    return "boolean"
  if tag['number'] != '1':
    return 'string'
  if tag['type'] == "Integer":
    return 'int32'
  if tag['type'] == "Float":
    return "double"
  return "string"

@dxpy.entry_point('recalibrateVariants')
def recalibrateVariants(**job_inputs):
    os.environ['CLASSPATH'] = '/opt/jar/AddOrReplaceReadGroups.jar:/opt/jar/GenomeAnalysisTK.jar:opt/jar/CreateSequenceDictionary.jar'
    
    dxpy.download_dxfile(job_inputs['reference_file'], "ref.fa")
    runAndCatchGATKError("java -Xmx4g net.sf.picard.sam.CreateSequenceDictionary REFERENCE=ref.fa OUTPUT=ref.dict", shell=True)
    
    numVariants = mergeVcfs(job_inputs['vcfs'])
    
    if numVariants > 0:
        
        recalibratedVariantsTable = dxpy.DXGTable(job_inputs['recalibrated_variants_table'])
        
        command = "java -Xmx12g org.broadinstitute.sting.gatk.CommandLineGATK -T VariantRecalibrator -input merged.sorted.vcf -tranchesFile model.tranches -recalFile model.recal -R ref.fa -rscriptFile model.plots.R -mode %s" % job_inputs["genotype_likelihood_model"]
    
        count = 0
        for resource in job_inputs.get('gatk_resources'):
            fh = dxpy.DXFile(resource)
            fileDetails = fh.get_details()
            fileName = "gatk_resource%d.%s.gz" %  (count, fileDetails['resource_type'].lower())
            dxpy.download_dxfile(resource, fileName)        
    
            p = subprocess.Popen(["tabix", "-f", "-p", fileDetails['resource_type'].lower(), fileName], stderr=subprocess.PIPE)
            if '[tabix] was bgzip' in p.communicate()[1]:
                subprocess.check_call(["mv",
                                       "gatk_resource%d.%s.gz" % (count, fileDetails['resource_type'].lower()),
                                       "gatk_resource%d.%s" % (count, fileDetails['resource_type'].lower())])
                fileName = "gatk_resource%d.%s" %  (count, fileDetails['resource_type'])
    
                
            command += " -resource:%s,%s,known=%s,training=%s,truth=%s,prior=%f %s" % (fileDetails['name'], fileDetails['resource_type'], str(fileDetails['known']).lower(), str(fileDetails['training']).lower(), str(fileDetails['truth']).lower(), fileDetails['prior'], fileName)    
            count += 1
                    
        if numVariants < 500000 and job_inputs["gatk_recalibration_model"] == None:
            if job_inputs["max_gaussians"] == None:
                job_inputs["max_gaussians"] = 4
            if job_inputs["fraction_bad"] == None:
                job_inputs["fraction_bad"] = 0.05
        if numVariants < 100000 and job_inputs["gatk_recalibration_model"] == None:
            if job_inputs["max_gaussians"] == None:
                job_inputs["max_gaussians"] = 2
            if job_inputs["fraction_bad"] == None:
                job_inputs["fraction_bad"] = 0.25
            print "There were very few variants. Consider adding additional variant set from the publicly available exome dataset."
                
        if job_inputs.get("max_gaussians") != None:
            command += " --maxGaussians %d" % job_inputs["max_gaussians"]
        else:
            command += " --maxGaussians 6"
        if job_inputs.get("max_iterations") != None:
            command += " --maxIterations %d" % job_inputs["max_iterations"]
        if job_inputs.get("num_k_means") != None:
            command += " --numKMeans %d" % job_inputs["num_k_means"]
        if job_inputs.get("std_threshold") != None:
            command += " --stdThreshold %f" % job_inputs["std_threshold"]
        if job_inputs.get("qual_threhsold") != None:
            command += " --qualThreshold %f" % job_inputs["qual_threshold"]
        if job_inputs.get("shrinkage") != None:
            command += " -shrinkage %f" % job_inputs["shrinkage"]
        if job_inputs.get("dirichlet") != None:
            command += " --dirichlet %f" % job_inputs["dirichlet"]
        if job_inputs.get("prior_counts") != None:
            command += " --priorCounts %d" % job_inputs["prior_counts"]
        if job_inputs.get("fraction_bad") != None:
            command += " -percentBad %f" % job_inputs["fraction_bad"]
        if job_inputs.get("min_num_bad_variants") != None:
            command += " -minNumBad %d" % job_inputs["min_num_bad_variants"]
        if job_inputs.get("ti_tv_target") != None:
            command += " -titv %f" % job_inputs["ti_tv_target"]
        if job_inputs.get("ignore_filter") != None:
            for x in job_inputs["ignore_filter"]:
                command += " -ignoreFilter %s" % x
        command += " -ts_filter_level %f" % job_inputs["ts_filter_level"]
        if job_inputs["trust_all_polymorphic"]:
            command += " -allPoly"
        if job_inputs.get('single_threaded') != True:
            command += " --num_threads " + str(cpu_count())
        
        annotations = []
        if job_inputs["genotype_likelihood_model"]:
            if job_inputs["variant_recalibrator_annotations"] != "" and not job_inputs["use_default_annotations"]:
                annotations = checkAnnotations(vcf)
                if len(annotations) == 0:
                    print "WARNING: Found no usable annotations, switching to default annotations"
        
        if job_inputs["use_default_annotations"] or annotations == []:
            if job_inputs["genotype_likelihood_model"] == "INDEL":
                annotations = checkAnnotations(open("merged.sorted.vcf", 'r'), ["QD", "FS", "HaplotypeScore", "ReadPosRankSum", "InbreedingCoeff"])
            else:
                annotations = checkAnnotations(open("merged.sorted.vcf", 'r'), ["QD", "FS", "HaplotypeScore", "MQRankSum", "ReadPosRankSum", "FS", "MQ", "InbreedingCoeff", "DP"])
    
        if job_inputs.get("gatk_recalibration_model") != None:
            dxpy.download_dxfile(dxpy.DXFile(job_inputs["gatk_recalibration_model"]).get_id(), "model.tar.gz")
            subprocess.check_call("tar -xvzf model.tar.gz", shell=True)
            i = 0
            while 1:
                try:
                    modelFile = open("model_file%d.vcf" % i, 'r')
                    modelFile.close()
                    command += " -input model_file%d.vcf" % i
                    i += 1
                except:
                    break
        
        for x in annotations:
            command += " -an " + x
    
    
        # Do variant recalibration model
        print command
        runAndCatchGATKError(command, shell=True)
        
        # Apply variant recalibration model
        
        runAndCatchGATKError("java -Xmx12g org.broadinstitute.sting.gatk.CommandLineGATK -T ApplyRecalibration -input merged.sorted.vcf -tranchesFile model.tranches -recalFile model.recal -R ref.fa -o recalibrated.vcf -ts_filter_level %s " % job_inputs["ts_filter_level"], shell = True)
        
        command = "dx_vcfToVariants2 --table_id %s --vcf_file recalibrated.vcf" % (recalibratedVariantsTable.get_id())
        if job_inputs['compress_reference']:
            command += " --compress_reference"
        if job_inputs['infer_no_call']:
            command += " --infer_no_call"
        if job_inputs['compress_no_call']:
            command += " --compress_no_call"
        print "Parsing Variants"
        subprocess.check_call(command, shell=True)

    output = {'ok': True}
    return output
    
def checkAnnotations(vcfFile, annotations):
    presentAnnotations = []
    count = 0
    for line in vcfFile:
        if line[0] != "#":
            entries = line.split("\t")[7]
            for x in entries.split(";"):
                if x.split("=")[0] in annotations:
                    presentAnnotations.append(x.split("=")[0])
                    annotations.remove(x.split("=")[0])
            count += 1
            if count > 10000 or len(annotations) == 0:
                break
    return presentAnnotations
    
def mergeVcfs(vcfs):
    
    command = "vcf-concat"
    present = False
    for i in range(len(vcfs)):
        if vcf[i] != "":
            dxpy.download_dxfile(dxpy.DXFile(vcfs[i]).get_id(), str(i)+".vcf")
            command += " %d.vcf" % i
            present = True
    command += " > merged.vcf"
        
    count = 0
    if present:
        subprocess.check_call(command, shell=True)
        for line in open("merged.vcf", 'r'):
            if line[0] != "#":   
                count += 1
            
        subprocess.check_call("vcfsorter.pl ref.dict merged.vcf > merged.sorted.vcf", shell=True)
        
    return count

@dxpy.entry_point('mapGatk')
def mapGatk(**job_inputs):
    os.environ['CLASSPATH'] = '/opt/jar/AddOrReplaceReadGroups.jar:/opt/jar/GenomeAnalysisTK.jar:opt/jar/CreateSequenceDictionary.jar'
    
    print job_inputs['interval']
    
    regionFile = open("regions.txt", 'w')
    regionFile.write(job_inputs['interval'])

    regionFile.close()
    for x in job_inputs['mappings_files']:
        dxpy.DXFile(x).wait_on_close()
    dxpy.DXFile(job_inputs['reference_file']).wait_on_close()

    for i in range(len(job_inputs['mappings_files'])):        
        dxpy.download_dxfile(job_inputs['mappings_files'][i], "input.%d.bam" % i)
        subprocess.check_call("samtools index input.%d.bam" % i, shell=True)
        job_inputs['command'] += " -I input.%d.bam" % i
    
    dxpy.DXFile(job_inputs['reference_file']).wait_on_close()
    dxpy.download_dxfile(job_inputs['reference_file'], "ref.fa")

    gatkIntervals = open("regions.interval_list", 'w')
    for x in re.findall("-L ([^:]*):(\d+)-(\d+)", job_inputs['interval']):
        gatkIntervals.write(x[0] + ":" + x[1] + "-" + x[2] + "\n")
    gatkIntervals.close()

    print "Indexing Reference"
    subprocess.check_call("samtools faidx ref.fa", shell=True)
    runAndCatchGATKError("java -Xmx4g net.sf.picard.sam.CreateSequenceDictionary REFERENCE=ref.fa OUTPUT=ref.dict" ,shell=True)

    command = job_inputs['command'] + job_inputs['interval']
    #print command
    print "In GATK"
    runAndCatchGATKError(command, shell=True)

    command = "dx_vcfToVariants2 --table_id %s --vcf_file output.vcf --region_file regions.txt" % (job_inputs['tableId'])
    if job_inputs['compress_reference']:
        command += " --compress_reference"
    if job_inputs['infer_no_call']:
        command += " --infer_no_call"
    if job_inputs['compress_no_call']:
        command += " --compress_no_call"

    file_id = dxpy.upload_local_file("output.vcf", wait_on_close=True).get_id()

    print "Parsing Variants"
    subprocess.check_call(command, shell=True)

    output = {}
    output['file_id'] = file_id
    output['ok'] = True

    return output

def splitUserInputRegions(jobRegions, inputRegions, prefix):
    jobList = re.findall("-L ([^:]*):(\d+)-(\d+)", jobRegions)
    inputList = re.findall("-L ([^:]*):(\d+)-(\d+)", inputRegions)
    
    result = ""
    for x in inputList:
        for y in jobList:
            if(x[0] == y[0]):
                lo = max(int(x[1]), int(y[1]))
                hi = min(int(x[2]), int(y[2]))
                if hi > lo:
                    result += " %s %s:%d-%d" % (prefix, x[0], lo, hi)
                    
    return result
