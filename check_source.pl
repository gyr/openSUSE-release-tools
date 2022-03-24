#! /usr/bin/perl

use File::Basename;
use File::Temp qw/ tempdir  /;
use XML::Simple;
use Data::Dumper;
use Cwd;
use Text::Diff;
BEGIN {
    unshift @INC, ($::ENV{'BUILD_DIR'} || '/usr/lib/build');
}
use Build;

my $ret = 0;

my $old = $ARGV[0];
my $dir = $ARGV[1];
my $bname = basename($dir);

for my $file (glob("$dir/_service:*")) {
    $file=basename($file);
    print "Found _service generated file $file in checkout. Please clean this up first.";
    $ret = 1;
}

my @specs = map basename($_), glob("$dir/*.spec");

if (@specs) {
    if (!-f "$dir/$bname.changes") {
        print "$bname.changes is missing. A package submitted as FooBar needs to have a FooBar.changes file with a format created by `osc vc`.\n";
        $ret = 1;
    }

    if (!-f "$dir/$bname.spec") {
        print "$bname.spec is missing. A package submitted as FooBar needs to have a FooBar.spec file.\n";
        $ret = 1;
    }
    exit($ret) if ($ret);
} else {
    # package without spec files, eg kiwi only
    exit($ret);
}

open(SPEC, "$dir/$bname.spec");
my $spec = join("", <SPEC>);
close(SPEC);

if ($spec !~ m/#[*\s]+Copyright\s/) {
    print "$bname.spec does not appear to contain a Copyright comment. Please stick to the format\n\n";
    print "# Copyright (c) 2011 Stephan Kulow\n\n";
    print "or use osc service runall format_spec_file\n";
    $ret = 1;
}

if ($spec =~ m/\nVendor:/) {
    print "$bname.spec contains a Vendor line, this is forbidden.\n";
    $ret = 1;
}

# Check that we have for each spec file a changes file - and that at least one
# contains changes
my $changes_updated = 0;
for my $spec (@specs) {
    $changes = $spec;
    $changes =~ s/\.spec$/.changes/;

    # new or deleted .changes files also count
    if ((-f "$old/$changes") != (-f "$dir/$changes")) {
        $changes_updated = 1;
        last;
    }
    elsif ((-f "$old/$changes") && (-f "$dir/$changes")) {
        if (system(("cmp", "-s", "$old/$changes", "$dir/$changes"))) {
            $changes_updated = 1;
            last;
        }
    }
}

if (!$changes_updated) {
    print "No changelog. Please use 'osc vc' to update the changes file(s).\n";
    $ret = 1;
}

if ($spec !~ m/\n%changelog\s/ && $spec != m/\n%changelog$/) {
    print "$bname.spec does not contain a %changelog line. We don't want a changelog in the spec file, but the %changelog section needs to be present\n";
    $ret = 1;
}

if ($spec !~ m/(#[^\n]*license)/i) {
    print "$bname.spec does not appear to have a license. The file needs to contain a free software license\n";
    print "Suggestion: use \"osc service runall format_spec_file\" to get our default license or\n";
    print "the minimal license:\n\n";
    print "# This file is under MIT license\n";
    $ret = 1;
}

my %patches = ();

for my $test (glob("/usr/lib/obs/service/source_validators/*")) {
    next if (!-f "$test");
    my $checkivsd = `/bin/bash $test --batchmode $dir $old < /dev/null 2>&1`;
    if ($?) {
        print "Source validator failed. Try \"osc service runall source_validator\"\n";
        print $checkivsd;
        print "\n";
        $ret = 1;
    }
    else {
        for my $line (split(/\n/, $checkivsd)) {
            # pimp up some warnings
            if ($line =~ m/Attention.*not mentioned/) {
                $line =~ s,\(W\) ,,;
                print "$line\n";
                $ret = 1;
            }
        }
    }
}

my $odir = getcwd();

chdir($dir) || die "chdir $dir failed";
for my $patch (glob("*.diff *.patch *.dif")) {
    $patches{$patch} = 'current';
}
chdir($odir) || die "chdir $odir failed";

if (-d "$old") {

    chdir($old) || die "chdir $old failed";
    my $cf = Build::read_config("x86_64", "/usr/lib/build/configs/default.conf");

    my %thash = ();
    for my $spec (glob("*.spec")) {
        my $ps = Build::Rpm::parse($cf, $spec);

        while (my ($k, $v) = each %$ps) {
            if ($k =~ m/^source/) {
                $thash{$v} = 1;
            }
        }
    }
    for my $patch (glob("*.diff *.patch *.dif")) {
        if ($patches{$patch}) {
            delete $patches{$patch};
        }
        else {
            $patches{$patch} = 'old';
        }
    }

    chdir($odir) || die "chdir $odir failed";
    chdir($dir) || die "chdir $dir failed";
    for my $spec (glob("*.spec")) {
        my $ps = Build::Rpm::parse($cf, $spec);
        open(OSPEC, "$spec");
        open(NSPEC, ">$spec.new");
        while (<OSPEC>) {
            chomp;
            if (m/^Source/) {
                my $line = $_;
                $line =~ s/^(Source[0-9]*)\s*:\s*//;
                if ($patches{$line}) {
                   delete $patches{$line};
                }
                my $prefix = $1;
                my $parsedline = $ps->{lc $prefix};
                if (defined $thash{$parsedline}) {
                    my $file = $line;
                    my $bname = basename($file);
                    print NSPEC "$prefix: $bname\n";
                }
                else {
                    print NSPEC "$_\n";
                }
            }
            else {
                print NSPEC "$_\n";
            }
        }
        close(OSPEC);
        close(NSPEC);
        system(("cp", "$spec", "$spec.beforeurlstrip"));
        rename("$spec.new", "$spec") || die "rename failed";
    }

    chdir($dir);
    my @changes = glob("*.changes");
    chdir($odir);

    if (%patches) {
        # parse changes
        for my $changes (@changes) {
            my $diff = "";
            if (! -e "$old/$changes") {
                $diff = diff "/dev/null", "$dir/$changes";
            }
            else {
                $diff = diff "$old/$changes", "$dir/$changes";
            }
            for my $line (split(/\n/, $diff)) {
                # Check if the line mentions a patch being added (starts with +)
                # or removed (starts with -)
                next unless $line =~ m/^[+-]/;
                # In any of those cases, remove the patch from the list
                $line =~ s/^[+-]//;
                for my $patch (keys %patches) {
                    if (index($line, $patch) != -1) {
                        delete $patches{$patch};
                    }
                }
            }
        }
    }
    # still some left?
    if (%patches) {
        $ret = 1;
        for my $patch (keys %patches) {
            # wording stolen from Raymond's declines :)
            if ($patches{$patch} eq 'current') {
                print "A patch ($patch) is being added without this addition being mentioned in the changelog.\n";
            }
            else {
                print "A patch ($patch) is being deleted without this removal being mentioned in the changelog.\n";
            }
        }
    }
}

my $tmpdir = tempdir("obs-XXXXXXX", TMPDIR => 1, CLEANUP => 1);
chdir($dir) || die 'tempdir failed';
if (system("/usr/lib/obs/service/download_files","--enforceupstream", "yes", "--enforcelocal", "yes", "--outdir", $tmpdir)) {
    print "Source URLs are not valid. Try \"osc service runall download_files\".\n";
    $ret = 2;
}

exit($ret);
